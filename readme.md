## Google Photos Takeout Metadata Restorer

yes, another google photos takeout fixer is here. I've been exporting from google photos regularly, thus understanding its little quirks on naming their files, which i've aimed to solve here. 

- Recursively scans a Takeout folder
- Finds sidecar JSON files and matches them to media
- Restores EXIF/GPS/time for images using ExifTool
- Restores creation_time for videos using FFmpeg
- Handles filename mismatches using "title" field from JSON
- Shows progress, prints failures, and writes a summary report

## Usage:
  python3 takeout_restore.py --root "/path/to/Takeout/Google Photos" --write
  python3 takeout_restore.py --root "/path/to/folder" --write --fix-names
  python3 takeout_restore.py --root "/path/to/folder" --dry-run

## Notes:
- exiftool is required (best-in-class for EXIF writing)
- ffmpeg is required for video timestamp restore

### Ubuntu:  
sudo apt update   
sudo apt install exiftool -y  
sudo apt install ffmpeg -y

### Mac: 
brew install exiftool  
brew install ffmpeg

### Windows: 
choco install exiftool  
choco install ffmpeg

