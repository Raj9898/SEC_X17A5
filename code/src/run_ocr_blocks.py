#!/usr/bin/env python
# coding: utf-8

"""
run_ocr_blocks.py: Script responsible for cleaning up AWS Textract errors (when we call Textract too fast).
Besides that it runs very much the same as run_ocr.py.
    1) OCRTextract.py
    2) OCRClean.py
"""

##################################
# LIBRARY/PACKAGE IMPORTS
##################################

import os
import json
import numpy as np
import pandas as pd
import time
from OCRTextract import textractParse, textractParse_pdfs_parallel, startJob
from OCRClean import clean_wrapper

from run_file_extraction import brokerFilter


##################################
# MAIN CODE EXECUTION
##################################

def main_p2_blocks(s3_bucket, s3_pointer, s3_session, temp_folder, input_pdf, input_png, 
            out_folder_raw_pdf, out_folder_raw_png, textract_obj, out_folder_clean_pdf, 
            out_folder_clean_png, rerun_job, broker_dealers):
    
    print('\n============\nStep 4 & 5 BIS: Fixing Block Error\n============\n')
    
    # ==============================================================================
    #               STEP 4 (Perform OCR via Textract on FOCUS Reports)
    # ==============================================================================
    
    # csv directory where we store balance sheet information 
    output_pdf_csvs = s3_session.list_s3_files(s3_bucket, out_folder_raw_pdf)
    
    # temp directory where JSON files is stored
    temp = s3_session.list_s3_files(s3_bucket, temp_folder)
    
    # s3 directory where we store the broker-dealer sliced filings 
    raw_pdf_files = s3_session.list_s3_files(s3_bucket, input_pdf)
    
    # ---------------------------------------------------------------------------
    # Load in Temp JSON files (FORM, TEXT, ERROR) if present from s3
    # ---------------------------------------------------------------------------
    
    if (temp_folder + 'X17A5-FORMS.json' in temp) and (rerun_job > 4):
        # retrieving downloaded files from s3 bucket
        s3_pointer.download_file(s3_bucket, temp_folder + 'X17A5-FORMS.json', 'temp.json')
        
        # read data on KEY-VALUE dictionary (i.e Textract FORMS) 
        with open('temp.json', 'r') as f: forms_dictionary = json.loads(f.read())
        
        # remove local files for JSON
        os.remove('temp.json')
    else:
        forms_dictionary = {}
    
    if (temp_folder + 'X17A5-TEXT.json' in temp) and (rerun_job > 4):
        # retrieving downloaded files from s3 bucket
        s3_pointer.download_file(s3_bucket, temp_folder + 'X17A5-TEXT.json', 'temp.json')
        
        # read data on TEXT-Confidence dictionary
        with open('temp.json', 'r') as f: text_dictionary = json.loads(f.read())  
            
        # remove local files for JSON
        os.remove('temp.json')
    else:
        text_dictionary = {}
    
    if (temp_folder + 'ERROR-TEXTRACT.json' in temp) and (rerun_job > 4):
        # retrieving downloaded files from s3 bucket
        s3_pointer.download_file(s3_bucket, temp_folder + 'ERROR-TEXTRACT.json', 'temp.json')
        
        # read data on errors derived from Textract
        with open('temp.json', 'r') as f: error_dictionary = json.loads(f.read()) 
            
        # remove local files for JSON
        os.remove('temp.json')
    else:
        error_dictionary = {}
    
    # ---------------------------------------------------------------------------
    # Perform Textract analysis on PDFs and PNGs
    # ---------------------------------------------------------------------------
    
    # trailing scaler for firms, keep track of missing
    prior_pdf_scaler = 1.0
    prior_png_scaler = 1.0
    prior_pdf_cik = np.nan
    prior_png_cik = np.nan
    
    # pdf directory where we store the broker-dealer information 
    textract_files = list(filter(lambda x: brokerFilter(broker_dealers, x), raw_pdf_files))
    number_files = len(textract_files)
    
    if "job_ids.json" in os.listdir():
        with open("job_ids.json", 'r') as f: job_ids = json.loads(f.read())
    else:
        job_ids = {}
 

    for counter, pdf_paths in enumerate(textract_files):
        # baseFile name to name export .csv file e.g. 1224385-2004-03-01.csv
        basefile = pdf_paths.split('/')[-1].split('-subset')[0]
        fileName = basefile + '.csv'
        print('\nPerforming OCR for %s (%d out of %s)' % (fileName,counter,number_files))
        
        # this try structure parses through the error dictionnary looking for 'Block' errors.
        # if basefile isn't a key (ie no errors in error_dictionary) then the try fails and we continue to the following pdf_path
        # if basefile is a key but the error is not block the code goes to the else clause and we countinue to the following pdf_path
        # this guarantees that the block after runs only for files with the block error
        try:
            if error_dictionary[basefile] == "'Blocks'":
                print(counter)
                print(error_dictionary[basefile])
            else:
                continue
        except:
            continue

        # if file is not found in output directory we extract the balance sheet
        # WE LOOK TO AVOID RE-RUNNING OLD TEXTRACT PARSES TO SAVE TIME, but if 
        # rerun_job is < 5 (True) we re-run Textract again
        if (out_folder_raw_pdf + fileName in output_pdf_csvs) and (rerun_job > 4):
            print('\t%s has been downloaded' % fileName)

        else:
            # run Textract OCR job and extract the parsed data 

            png_paths = input_png + basefile + '/'

            pdf_df, png_df, forms_data, text_data, error = textractParse_pdfs_parallel(pdf_paths, s3_bucket, job_ids[basefile])

            # if no error is reported we save FORMS, TEXT, DataFrame
            if error is None:

                # store accompanying information for JSONs
                forms_dictionary[basefile] = forms_data
                text_dictionary[basefile]  = text_data
                error_dictionary.pop(basefile)
                
                # writing data table to .csv file
                pdf_df.to_csv(fileName, index=False)
                with open(fileName, 'rb') as data:
                    s3_pointer.put_object(Bucket=s3_bucket, Key=out_folder_raw_pdf + fileName, Body=data)

                # writing data frame to .csv file extracted from PNG
                if png_df is not None:
                    png_df.to_csv(fileName, index=False)
                    with open(fileName, 'rb') as data:
                        s3_pointer.put_object(Bucket=s3_bucket, Key=out_folder_raw_png + fileName, Body=data)

                print('--------------------------------------------------------------------')
                print('\tSaved %s file to s3 bucket' % fileName)

                # ==============================================================================
                #               STEP 5 (Perform Cleaning Operations on Textract Table)
                # ==============================================================================

                if pdf_df is not None:
                    print('\tWorking on PDF balance-sheet')
                    # perform cleaning operations on read balance sheets for PDF and PNGs

                    # adding following try structure. In rare cases clean_wrapper has an error due to invalid cleaning of pdf dataframe                               that raises an error (for dataframe '1139137-2006-02-28.csv')
                    try:
                        pdf_df_clean, prior_pdf_scaler, prior_pdf_cik = clean_wrapper(pdf_df, text_dictionary, basefile, fileName,
                                                                                      prior_pdf_scaler, prior_pdf_cik)

                        # export contents to the s3 directory
                        pdf_df_clean.to_csv(fileName, index=False)
                        with open(fileName, 'rb') as data:
                            s3_pointer.put_object(Bucket=s3_bucket, Key=out_folder_clean_pdf + fileName, Body=data)

                    except Exception as e:
                        error_dictionary[basefile] = str(e)

                if png_df is not None:
                    print('\tWorking on PNG balance-sheet')
                    png_df_clean, prior_png_scaler, prior_png_cik = clean_wrapper(png_df, text_dictionary, basefile, fileName,
                                                                                  prior_png_scaler, prior_png_cik)

                    png_df_clean.to_csv(fileName, index=False)
                    with open(fileName, 'rb') as data:
                        s3_pointer.put_object(Bucket=s3_bucket, Key=out_folder_clean_png + fileName, Body=data)

                # remove local file after it has been created
                if os.path.isfile(fileName):
                    os.remove(fileName)
                    print('--------------------------------------------------------------------\n')

            else:
                print('\tError with Textract : '+ error)
                error_dictionary[basefile] = error
                    
           
    # ---------------------------------------------------------------------------
    # Save JSON files for updated figures (FORM, TEXT, ERROR)
    # ---------------------------------------------------------------------------
    
    # write to a JSON file for FORMS 
    with open('X17A5-FORMS.json', 'w') as file: 
        json.dump(forms_dictionary, file)
        file.close()
    
    # save contents to AWS S3 bucket
    with open('X17A5-FORMS.json', 'rb') as data: 
        s3_pointer.upload_fileobj(data, s3_bucket, temp_folder + 'X17A5-FORMS.json')
    os.remove('X17A5-FORMS.json')

    # write to a JSON file for TEXT 
    with open('X17A5-TEXT.json', 'w') as file: 
        json.dump(text_dictionary, file)
        file.close()
    
    # save contents to AWS S3 bucket
    with open('X17A5-TEXT.json', 'rb') as data: 
        s3_pointer.upload_fileobj(data, s3_bucket, temp_folder + 'X17A5-TEXT.json')
    os.remove('X17A5-TEXT.json')

    # write to a JSON file for FORMS 
    with open('ERROR-TEXTRACT.json', 'w') as file: 
        json.dump(error_dictionary, file)
        file.close()
    
    # save contents to AWS S3 bucket
    with open('ERROR-TEXTRACT.json', 'rb') as data: 
        s3_pointer.upload_fileobj(data, s3_bucket, temp_folder + 'ERROR-TEXTRACT.json')
    os.remove('ERROR-TEXTRACT.json')