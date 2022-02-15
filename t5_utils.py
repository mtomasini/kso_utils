#t5 utils
import argparse, os
import kso_utils.db_utils as db_utils
import pandas as pd
import numpy as np
import math
import subprocess
import shutil
import logging
import pims
import cv2

from tqdm import tqdm
from PIL import Image
from IPython.display import HTML, display, update_display, clear_output
import ipywidgets as widgets
from ipywidgets import interact, Layout
from kso_utils.zooniverse_utils import auth_session, populate_agg_annotations
import kso_utils.tutorials_utils as t_utils
import kso_utils.server_utils as s_utils
import kso_utils.t12_utils as t12
import kso_utils.koster_utils as k_utils
import kso_utils.spyfish_utils as spyfish_utils
from ipyfilechooser import FileChooser
from pathlib import Path
from datetime import date

from panoptes_client import (
    SubjectSet,
    Subject,
    Project,
    Panoptes,
)

# Logging

logging.basicConfig(level=logging.WARNING)
logger = logging.getLogger(__name__)
logger.setLevel(logging.DEBUG)
out_df = pd.DataFrame()

# Function to set up and collect project-specific information
def setup_frame_info(project_name: str):
    # Initiate db
    db_info_dict = t_utils.initiate_db(project_name)
    movie_folder = t_utils.get_project_info(project_name, "movie_folder")
    zoo_number = t_utils.get_project_info(project_name, "Zooniverse_number")
    if str(zoo_number).isdigit():
        # Connect to Zooniverse project
        zoo_project = t_utils.connect_zoo_project(project_name)
        zoo_info_dict = t_utils.retrieve__populate_zoo_info(project_name = project_name, 
                                                    db_info_dict = db_info_dict,
                                                    zoo_project = zoo_project,
                                                    zoo_info = ["subjects", "classifications", "workflows"])
    else:
        zoo_project, zoo_info_dict = None, None
    return db_info_dict, zoo_project, zoo_info_dict

# Function to select the species of interest from those available
def choose_species(db_path: str = "koster_lab.db"):
    conn = db_utils.create_connection(db_path)
    species_list = pd.read_sql_query("SELECT label from species", conn)["label"].tolist()
    if len(species_list) == 0:
        species_list = [""]
        logging.error("Your database contains no species, please add at least one species before continuing.")
    w = widgets.SelectMultiple(
        options=species_list,
        value=[species_list[0]],
        description='Species',
        disabled=False
    )

    display(w)
    return w

#Function to choose a folder path
def choose_folder():
    fc = FileChooser('.')
    display(fc)
    return fc

# Function to match species selected to species id
def get_species_ids(project_name: str, species_list: list):
    """
    # Get ids of species of interest
    """
    db_path = t_utils.get_project_info(project_name, "db_path")
    conn = db_utils.create_connection(db_path)
    if len(species_list) == 1:
        species_ids = pd.read_sql_query(
            f'SELECT id FROM species WHERE label=="{species_list[0]}"', conn
        )["id"].tolist()
    else:
        species_ids = pd.read_sql_query(
        f'SELECT id FROM species WHERE label IN "{tuple(species_list)}"', conn
    )["id"].tolist()
    return species_ids
    
def get_species_frames(agg_clips_df, species_ids: list, server_dict: dict, conn, project_name, n_frames_subject):
    """
    # Function to identify up to n number of frames per classified clip
    # that contains species of interest after the first time seen

    # Find classified clips that contain the species of interest
    """
        
    # Retrieve list of subjects
    subjects_df = pd.read_sql_query(
                    f"SELECT id, clip_start_time, movie_id FROM subjects WHERE subject_type='clip'",
                    conn,)

    # Combine the aggregated clips and subjects dataframes
    frames_df = pd.merge(agg_clips_df, subjects_df, how="left", left_on="subject_ids", right_on="id").drop(columns=["id"])

    # Identify the second of the original movie when the species first appears
    frames_df["first_seen_movie"] = (
        frames_df["clip_start_time"] + frames_df["first_seen"]
    )
            
    # Get the filepath and fps info of the original movies
    f_paths = pd.read_sql_query(f"SELECT id, filename, fpath, fps FROM movies", conn)        
            
    server = t_utils.get_project_info(project_name, "server")
    
    if server == "SNIC" and project_name == "Koster_Seafloor_Obs":
        
        movie_folder = t_utils.get_project_info(project_name, "movie_folder")
          
        f_paths["fpath"] = movie_folder + f_paths["fpath"]

        # Ensure swedish characters don't cause issues
        f_paths["fpath"] = f_paths["fpath"].apply(k_utils.unswedify)
        
        # Include movies' filepath and fps to the df
        frames_df = frames_df.merge(f_paths, left_on="movie_id", right_on="id")
        
        # Specify if original movies can be found
        # frames_df["fpath"] = frames_df["fpath"].apply(lambda x: x.encode('utf-8'))
        movie_paths = [k_utils.unswedify(str(Path(movie_folder, x))) for x in s_utils.get_snic_files(server_dict["client"], movie_folder).spath.values]
        frames_df["exists"] = frames_df["fpath"].apply(lambda x: True if x in movie_paths else False)
                                                      
        if len(frames_df[~frames_df.exists]) > 0:
            logging.error(
                f"There are {len(frames_df) - frames_df.exists.sum()} out of {len(frames_df)} frames with a missing movie"
            )

        # Select only frames from movies that can be found
        frames_df = frames_df[frames_df.exists]

    if server == "AWS":
        
        # Include movies' filepath and fps to the df
        frames_df = frames_df.merge(f_paths, left_on="movie_id", right_on="id")
        
        ##### Add species_id info ####
        # Retrieve species info
        species_ids = pd.read_sql_query(
            f"SELECT id, label, scientificName FROM species",
            conn,)

        # Retrieve species info
        species_ids = species_ids.rename(columns={"id": "species_id"})
        
        # Match format of species name to Zooniverse labels
        species_ids["label"] = species_ids["label"].str.upper()
        species_ids["label"] = species_ids["label"].str.replace(" ", "")
        
        # Combine the aggregated clips and subjects dataframes
        frames_df = pd.merge(frames_df, species_ids, how="left", on="label").drop(columns=["id"])
  
    # Identify the ordinal number of the frames expected to be extracted
    frames_df["frame_number"] = frames_df[["first_seen_movie", "fps"]].apply(
        lambda x: [
            int((x["first_seen_movie"] + j) * x["fps"]) for j in range(n_frames_subject)
        ], 1
    )

    # Reshape df to have each frame as rows
    lst_col = "frame_number"

    frames_df = pd.DataFrame(
        {
            col: np.repeat(frames_df[col].values, frames_df[lst_col].str.len())
            for col in frames_df.columns.difference([lst_col])
        }
    ).assign(**{lst_col: np.concatenate(frames_df[lst_col].values)})[
        frames_df.columns.tolist()
    ]

    # Drop unnecessary columns
    frames_df.drop(["subject_ids"], inplace=True, axis=1)
        
        
    return frames_df

# Function to gather information of frames already uploaded
def check_frames_uploaded(frames_df: pd.DataFrame, project_name, species_ids, conn):
    # Get info of frames of the species of interest already uploaded
    if len(species_ids) <= 1:
        uploaded_frames_df = pd.read_sql_query(f"SELECT movie_id, frame_number, frame_exp_sp_id FROM subjects WHERE frame_exp_sp_id=='{species_ids[0]}' AND subject_type='frame'", conn)
    
    else:
        uploaded_frames_df = pd.read_sql_query(
        f"SELECT movie_id, frame_number, frame_exp_sp_id FROM subjects WHERE frame_exp_sp_id IN {tuple(species_ids)} AND subject_type='frame'",
    conn,
    )
    
    # Filter out frames that have already been uploaded
    if len(uploaded_frames_df) > 0:
        print("There are some frames already uploaded in Zooniverse for the species selected. Checking if those are the frames you are trying to upload")
        merge_df = pd.merge(frames_df, uploaded_frames_df, 
                            left_on=["movie_id", "frame_number"], 
                            right_on=["movie_id", "frame_number"],
                            how='left', indicator=True)['_merge'] == 'both'

        # Exclude frames that have already been uploaded
        frames_df = frames_df[merge_df == False]
        if len(frames_df) == 0:
            logging.error("All of the frames you have selected are already uploaded.")
        else:
            print("There are", len(frames_df), "frames with the species of interest not uploaded to Zooniverse yet.")
            
    else:
        print("There are no frames uploaded in Zooniverse for the species selected.")
            
    return frames_df



# Function to extract selected frames from videos
def extract_frames(df, server, server_dict, project_name, frames_folder):
    """
    Extract frames and save them in chosen folder.
    """
    
    #WIP
    if server == "AWS":
        
        # Extract the key of each movie
        df['key_movie'] = df['fpath'].apply(lambda x: x.split('/',3)[3])
        
        # Set the filename of the frames
        df["frame_path"] = (
            frames_folder
            + df["filename"].astype(str)
            + "_frame_"
            + df["frame_number"].astype(str)
            + "_"
            + df["label"].astype(str)
            + ".jpg"
        )
        
        for key_movie in df["key_movie"].unique():
            # generate a temp url for the movie 
            url = server_dict['client'].generate_presigned_url('get_object', 
                                                            Params = {'Bucket': server_dict['bucket'], 
                                                                      'Key': key_movie}, 
                                                            ExpiresIn = 200)
            if url is None:
                logging.error(f"Movie {key_movie} couldn't be found in the server.")
            else:
                
                # Select the frames to download from the movie
                key_movie_df =  df[df['key_movie'] == key_movie].reset_index()

                # Read the movie on cv2 and prepare to extract frames
                cap = cv2.VideoCapture(url)

                # Get the frame numbers for each movie the fps and duration
                for index, row in tqdm(key_movie_df.iterrows(), total=key_movie_df.shape[0]):
                    # Create the folder to store the frames if not exist
                    if not os.path.exists(row["frame_path"]):
                        cap.set(1, row["frame_number"])
                        ret, frame = cap.read()
                        cv2.imwrite(row["frame_path"], frame)
       
    
    if server == "SNIC":
        movie_folder = t_utils.get_project_info(project_name, "movie_folder")
        movie_files = s_utils.get_snic_files(server_dict["client"], movie_folder)["spath"].tolist()

        # Create the folder to store the frames if not exist
        if not os.path.exists(frames_folder):
            os.mkdir(frames_folder)

        # Get movies filenames from their path
        df["movie_filename"] = df["fpath"].apply(lambda x: os.path.splitext(os.path.basename(x))[0])

        # Set the filename of the frames
        df["frame_path"] = (
            frames_folder
            + df["movie_filename"].astype(str)
            + "_frame_"
            + df["frame_number"].astype(str)
            + "_"
            + df["species_id"].astype(str)
            + ".jpg"
        )


        # Download movies that are not available locally
        if len(df["fpath"].unique()) > 5:
            logging.error(f"You are about to download {len(df['fpath'].unique())} movies to your local machine. We recommend running this notebook on your SNIC server environment directly instead to limit transfer volume.")

        else:
            for k in df["fpath"].unique():
                if not os.path.exists(k):
                    print(k, (os.path.basename(k) not in movie_files), (k_utils.reswedify(os.path.basename(k)) not in movie_files), (k_utils.reswedify(os.path.basename(k)) not in movie_files), os.path.basename(k).encode('utf-8'), k_utils.reswedify(os.path.basename(k)).encode('utf-8'), k_utils.unswedify(os.path.basename(k)).encode('utf-8'))
                    if os.path.basename(k) not in movie_files and k_utils.reswedify(os.path.basename(k)) in movie_files:
                        k = k_utils.reswedify(k)
                    elif os.path.basename(k) not in movie_files and k_utils.reswedify(os.path.basename(k)) not in movie_files:
                        k = k_utils.unswedify(k)
                    # Download the movie of interest
                    s_utils.download_object_from_snic(
                                    server_dict["sftp_client"],
                                    remote_fpath=k,
                                    local_fpath=str(Path(".", k_utils.unswedify(os.path.basename(k))))
                    )

            video_dict = {k: pims.Video(str(Path(".", k_utils.unswedify(os.path.basename(k))))) for k in df["fpath"].unique()}

            # Save the frame as matrix
            df["frames"] = df[["fpath", "frame_number"]].apply(
                lambda x: video_dict[x["fpath"]][int(x["frame_number"])],
                1,
            )

            # Extract and save frames
            for frame, filename in zip(df["frames"], df["frame_path"]):
                Image.fromarray(frame).save(f"{filename}")

            print("Frames extracted successfully")
    
    return df

# Function to the provide drop-down options to select the frames to be uploaded
def get_frames(species_names: list, db_path: str, zoo_info_dict: dict, server_dict: dict, project_name: str, n_frames_subject=3, subsample_up_to=100):
    
    ### Transform species names to species ids##
    if species_names[0] == "":
        logging.error("No species were selected. Please select at least one species before continuing.")
        
    else:
        species_ids = get_species_ids(project_name, species_names)
        
    
    ### Retrieve project-specific information and connect to db
    movie_folder = t_utils.get_project_info(project_name, "movie_folder")
    server = t_utils.get_project_info(project_name, "server")
    conn = db_utils.create_connection(db_path)
    
    if movie_folder == "None" and server != "AWS":
        # Extract frames of interest from a folder with frames
        df = FileChooser('.')
        df.title = '<b>Select frame folder location</b>'
            
        # Callback function
        def build_df(chooser):
            frame_files = os.listdir(chooser.selected)
            frame_paths = [chooser.selected+i for i in frame_files]
            try:
                os.symlink(chooser.selected[:-1], 'linked_frames')
            except FileExistsError:
                os.remove('linked_frames')
                os.symlink(chooser.selected[:-1], 'linked_frames')
            chooser.df = pd.DataFrame(frame_paths, columns=["frame_path"])
            # TODO: Add multiple species option
            chooser.df["species_id"] = species_ids
                
        # Register callback function
        df.register_callback(build_df)
        display(df)
        
    else:
        ## Choose the Zooniverse workflow/s with classified clips to extract the frames from ####
        # Select the Zooniverse workflow/s of interest
        workflows_out = t12.WidgetMaker(zoo_info_dict["workflows"])
        display(workflows_out)
        
        # Select the agreement threshold to aggregrate the responses
        agg_params = t12.choose_agg_parameters("clip")

        # Select the temp location to store frames before uploading them to Zooniverse
        df = FileChooser('.')
        df.title = '<b>Choose location to store frames</b>'
            
        # Callback function
        def extract_files(chooser):
            # Get the aggregated classifications based on the specified aggrement threshold
            clips_df = t12.get_classifications(workflows_out.checks,
                                               zoo_info_dict["workflows"], "clip", zoo_info_dict["classifications"], db_path)
        
            agg_clips_df, raw_clips_df = t12.aggregrate_classifications(clips_df, "clip", project_name, agg_params=agg_params)
#             agg_clips_df = agg_clips_df.rename(columns={"frame_exp_sp_id": "species_id"})
            
            # Match format of species name to Zooniverse labels
            species_names_zoo = [species_name.upper() for species_name in species_names]
            species_names_zoo = [species_name.replace(" ", "") for species_name in species_names_zoo]
            
            # Select only aggregated classifications of species of interest:
            sp_agg_clips_df = agg_clips_df[agg_clips_df["label"].isin(species_names_zoo)]
            
            # Subsample up to desired sample
            if sp_agg_clips_df.shape[0] >= subsample_up_to:
                print ("Subsampling up to", subsample_up_to)
                sp_agg_clips_df = sp_agg_clips_df.sample(subsample_up_to)
            
            # Populate the db with the aggregated classifications
            populate_agg_annotations(sp_agg_clips_df, "clip", project_name)
            
            # Get df of frames to be extracted
            frame_df = get_species_frames(sp_agg_clips_df, species_ids, server_dict, conn, project_name, n_frames_subject)
            
            # Check the frames haven't been uploaded to Zooniverse
            frame_df = check_frames_uploaded(frame_df, project_name, species_ids, conn)
            
            # Extract the frames from the videos and store them in the temp location
            chooser.df = extract_frames(frame_df, server, server_dict, project_name, chooser.selected)
            try:
                os.symlink(chooser.selected[:-1], 'linked_frames')
            except FileExistsError:
                os.remove('linked_frames')
                os.symlink(chooser.selected[:-1], 'linked_frames')
                
        # Register callback function
        df.register_callback(extract_files)
        display(df)
        
    return df


# Function to specify the frame modification
def select_modification():
    # Widget to select the clip modification
    
    frame_modifications = {"Color_correction": {
        "-vf": "curves=red=0/0 0.396/0.67 1/1:green=0/0 0.525/0.451 1/1:blue=0/0 0.459/0.517 1/1,scale=1280:-1",#borrowed from https://www.element84.com/blog/color-correction-in-space-and-at-sea                         
        "-pix_fmt": "yuv420p",
        "-preset": "veryfast"
        }, "Zoo_no_compression": {                 
        "-pix_fmt": "yuv420p",
        "-preset": "veryfast"
        }, "Zoo_low_compression": {
        "-crf": "30",                    
        "-pix_fmt": "yuv420p",
        "-preset": "veryfast"
        }, "Zoo_medium_compression": {
        "-crf": "27",                   
        "-pix_fmt": "yuv420p",
        "-preset": "veryfast"
        }, "Zoo_high_compression": {
        "-crf": "25",                     
        "-pix_fmt": "yuv420p",
        "-preset": "veryfast"
        }, "Blur_sensitive_info": {
        "-crf": "30",
        "-filter_complex": "[0:v]crop=iw:ih*(15/100):0:0,boxblur=luma_radius=min(w\,h)/5:chroma_radius=min(cw\,ch)/5:luma_power=1[b0]; \
        [0:v]crop=iw:ih*(15/100):0:ih*(95/100),boxblur=luma_radius=min(w\,h)/5:chroma_radius=min(cw\,ch)/5:luma_power=1[b1]; \
        [0:v][b0]overlay=0:0[ovr0]; \
        [ovr0][b1]overlay=0:H*(95/100)[ovr1]",   
        "-map": "[ovr1]",
        "-pix_fmt": "yuv420p",
        "-preset": "veryfast"
        }, "None": {}}
    
    select_modification_widget = widgets.Dropdown(
                    options=[(a,b) for a,b in frame_modifications.items()],
                    description="Select frame modification:",
                    ensure_option=True,
                    disabled=False,
                    style = {'description_width': 'initial'}
                )
    
    display(select_modification_widget)
    return select_modification_widget


def check_frame_size(frame_paths):
    
    # Get list of files with size
    files_with_size = [ (file_path, os.stat(file_path).st_size) 
                        for file_path in frame_paths]

    df = pd.DataFrame(files_with_size, columns=["File_path","Size"])

    #Change bytes to MB
    df['Size'] = df['Size']/1000000
    
    if df['Size'].ge(1).any():
        print("Frames are too large (over 1 MB) to be uploaded to Zooniverse. Compress them!")
        return df
    else:
        print("Frames are a good size (below 1 MB). Ready to be uploaded to Zooniverse")
        return df
    
# Function to compare original to modified frames
def compare_frames(df):
    
    if not isinstance(df, pd.DataFrame):
        df = df.df

    # Save the paths of the clips
    original_frame_paths = df["frame_path"].unique()
    
    # Add "no movie" option to prevent conflicts
    original_frame_paths = np.append(original_frame_paths,"No frame")
    
    clip_path_widget = widgets.Dropdown(
                    options=tuple(np.sort(original_frame_paths)),
                    description="Select original frame:",
                    ensure_option=True,
                    disabled=False,
                    layout=Layout(width='50%'),
                    style = {'description_width': 'initial'}
                )
    
    main_out = widgets.Output()
    display(clip_path_widget, main_out)
    
    # Display the original and modified clips
    def on_change(change):
        with main_out:
            clear_output()
            if change["new"]=="No frame":
                print("It is OK to modify the frames again")
            else:
                a = view_frames(df, change["new"])
                display(a)
                   
    clip_path_widget.observe(on_change, names='value')
    
# Display the frames using html
def view_frames(df, frame_path):
    
    # Get path of the modified clip selected
    modified_frame_path = df[df["frame_path"]==frame_path].modif_frame_path.values[0]
    based_dir = "linked_frames"
    frame_path = os.path.basename(frame_path)
        
    html_code = f"""
        <html>
        <div style="display: flex; justify-content: space-around">
        <div>
          <img src='{os.path.join(based_dir, frame_path)}'>
        </img>
        </div>
        <div>
          <img src='{modified_frame_path}'>
        </img>
        </div>
        </html>"""
   
    return HTML(html_code)

    
# Function modify the frames
def modify_frames(frames_to_upload_df, species_i, frame_modification, modification_details):

    # Specify the folder to host the modified clips
    mod_frames_folder = "modified_" + "_".join(species_i) +"_frames"
    
    # Specify the path of the modified clips
    frames_to_upload_df["modif_frame_path"] = str(Path(mod_frames_folder, "modified")) + frames_to_upload_df["frame_path"].apply(lambda x: os.path.basename(x))
    
    # Remove existing modified clips
    if os.path.exists(mod_frames_folder):
        shutil.rmtree(mod_frames_folder)

    if not frame_modification=="None":
        
        # Save the modification details to include as subject metadata
        frames_to_upload_df["frame_modification_details"] = str(modification_details)
        
        # Create the folder to store the videos if not exist
        if not os.path.exists(mod_frames_folder):
            os.mkdir(mod_frames_folder)

        #### Modify the clips###
        # Read each clip and modify them (printing a progress bar) 
        for index, row in tqdm(frames_to_upload_df.iterrows(), total=frames_to_upload_df.shape[0]): 
            if not os.path.exists(row['modif_frame_path']):
                if "-vf" in modification_details:
                    subprocess.check_output(["ffmpeg",
                                     "-i", str(row['frame_path']),
                                     "-vf", modification_details["-vf"],
                                     "-pix_fmt", modification_details["-pix_fmt"],
                                     "-preset", modification_details["-preset"],
                                     str(row['modif_frame_path'])])
                elif "-crf" in modification_details:
                    subprocess.call(["ffmpeg",
                                     "-i", str(row['frame_path']),
                                     "-crf", modification_details["-crf"],
                                     "-pix_fmt", modification_details["-pix_fmt"],
                                     "-preset", modification_details["-preset"],
                                     str(row['modif_frame_path'])])
                elif "-filter_complex" in modification_details:
                    subprocess.call(["ffmpeg",
                                     "-i", str(row['frame_path']),
                                     "-filter_complex", modification_details["-filter_complex"],
                                     "-crf", modification_details["-crf"],
                                     "-pix_fmt", modification_details["-pix_fmt"],
                                     "-preset", modification_details["-preset"],
                                     "-map", modification_details["-map"],
                                     str(row['modif_frame_path'])])       
                else:
                    subprocess.call(["ffmpeg",
                                     "-i", str(row['frame_path']),
                                     "-pix_fmt", modification_details["-pix_fmt"],
                                     "-preset", modification_details["-preset"],
                                     str(row['modif_frame_path'])])


        print("Frames modified successfully")
        
    else:
        
        # Save the modification details to include as subject metadata
        frames_to_upload_df["modif_frame_path"] = frames_to_upload_df["frame_path"]
        
    return frames_to_upload_df
 

# Function to set the metadata of the frames to be uploaded to Zooniverse
def set_zoo_metadata(df, species_list, project_name, db_info_dict):
    
    if not isinstance(df, pd.DataFrame):
        df = df.df

    if "modif_frame_path" in df.columns and "no_modification" not in df["modif_frame_path"].values:
        df["frame_path"] = df["modif_frame_path"]

    # Set project-specific metadata
    if project_name == "Koster_Seafloor_Obs":
        upload_to_zoo = df[["frame_path", "species_id", "movie_id"]]
    
        movies_df = pd.read_csv(db_info_dict["local_movies_csv"])
    
        upload_to_zoo = upload_to_zoo.merge(movies_df, left_on="movie_id",
                                            right_on="movie_id")
        
    elif project_name == "SGU":
        upload_to_zoo = df[["frame_path", "species_id", "filename"]]
        
    elif project_name == "Spyfish_Aotearoa":    
        upload_to_zoo = spyfish_utils.spyfish_subject_metadata(df, db_info_dict)
        
    
    # Add information about the type of subject
    upload_to_zoo["subject_type"] = "frame"
    upload_to_zoo = upload_to_zoo.rename(columns={"species_id": "frame_exp_sp_id"})
        
    return upload_to_zoo

    

# Function to upload frames to Zooniverse
def upload_frames_to_zooniverse(upload_to_zoo, species_list, db_info_dict, project_name, project):
    
    # Estimate the number of frames
    n_frames = upload_to_zoo.shape[0]
    
    if project_name == "Koster_Seafloor_Obs":
        created_on = upload_to_zoo["created_on"].unique()[0]
        sitename = upload_to_zoo["siteName"].unique()[0]
        
        # Name the subject set
        subject_set_name = "frames_" + str(int(n_frames)) + "_"+ "_".join(species_list) + "_" + sitename + "_" + created_on
        
    elif project_name == "SGU":
        surveys_df = pd.read_csv(db_info_dict["local_surveys_csv"])
        sites_df = pd.read_csv(db_info_dict["local_sites_csv"])
        created_on = surveys_df["SurveyDate"].unique()[0]
        folder_name = os.path.split(os.path.dirname(upload_to_zoo["frame_path"].iloc[0]))[1]
        sitename = folder_name
        
        # Name the subject set
        subject_set_name = "frames_" + str(int(n_frames)) + "_"+ "_".join(species_list) + "_" + sitename + "_" + created_on
        
    else:
        # Name the subject for frames from multiple sites/movies
        subject_set_name = "frames_" + str(int(n_frames)) + "_" + "_".join(species_list) + date.today().strftime("_%d_%m_%Y")
        
    
    # Create a new subject set to host the frames
    subject_set = SubjectSet()

    subject_set.links.project = project
    subject_set.display_name = subject_set_name

    subject_set.save()

    print(subject_set_name, "subject set created")

    # Save the df as the subject metadata
    subject_metadata = upload_to_zoo.set_index('frame_path').to_dict('index')

    # Upload the clips to Zooniverse (with metadata)
    new_subjects = []

    print("uploading subjects to Zooniverse")
    for frame_path, metadata in tqdm(subject_metadata.items(), total=len(subject_metadata)):
        subject = Subject()
        
        
        subject.links.project = project
        subject.add_location(frame_path)
        
        print(frame_path)
        subject.metadata.update(metadata)
        
        print(metadata)
        subject.save()
        print("subject saved")
        new_subjects.append(subject)

    # Upload videos
    subject_set.add(new_subjects)
    print("Subjects uploaded to Zooniverse")
    