# TEA TAPR Data Scraper - Documentation

This project contains a custom web scraper designed to automate the download of **Texas Academic Performance Report (TAPR)** data from the Texas Education Agency (TEA) website.

## Files in this Directory

*   **`tapr_scraper_full.py`**: The primary script. It is designed to iterate through all school years, geographic levels, and data categories to download the complete TAPR dataset as CSV files.
*   **`tapr_example_district.py`**: A simplified example script that downloads only the "District Reference" category for the year 2024. Use this to verify your connection and understand the logic.

## Prerequisites

1.  **Python 3**: Ensure you have Python 3.6 or higher installed.
2.  **Requests Library**: The scripts use the `requests` library to handle HTTP communication.
    ```bash
    pip install requests
    ```

## How to Run

### Running the Example
To verify the scraper works on your machine, run the example script:
```bash
python3 tapr_example_district.py
```
This will create a `tapr_example/` folder and download a single CSV file.

### Running the Full Scrape
To start downloading the entire TAPR database:
```bash
python3 tapr_scraper_full.py
```
**Note:** This process will take a very long time (hours or potentially days) due to the thousands of files being requested and the built-in politeness delays.

---

## How to Modify the Script

### 1. Changing the Year Range
At the bottom of `tapr_scraper_full.py`, look for the `available_years` variable. 
*   **Current setting:** `range(2013, 2026)` (Downloads 2012-13 through 2024-25).
*   **To download only recent years:** Change it to `range(2022, 2026)`.

### 2. Filtering Geographic Levels
The `available_levels` list controls which aggregate data you pull:
*   `all_c`: All Campuses
*   `all_d`: All Districts
*   `all_r`: All Regions
*   `all_co`: All Counties
*   `all_s`: Statewide

If you only want Campus and District data, modify the list to:
`available_levels = ["all_c", "all_d"]`

### 3. Adjusting the Download Speed
In the `run()` method, there is a `time.sleep(1)` command. 
*   **To go faster:** You can reduce this to `0.5`, but be aware that TEA's servers may block your IP if you send requests too rapidly.
*   **To be safer:** Increase this to `2` or `3` seconds.

### 4. Changing the Output Folder
When initializing the scraper, you can change the destination folder:
```python
scraper = TaprScraper(output_dir="my_tapr_data")
```

## Data Structure
The script automatically organizes the downloads into a logical folder hierarchy:
`tapr_data/ [Year] / [Level] / [Filename].csv`

Example:
`tapr_data/2024/Campuses/2024 Campus Student Information.csv`

## Troubleshooting
*   **Connection Errors:** If the script stops, it usually means the TEA server timed out. You can simply restart the script; it will overwrite existing files but resume the process.
*   **Missing Categories:** TEA occasionally changes category names between years. This script uses dynamic discovery to find whatever is available for that specific year, so it is highly adaptable to site changes.
