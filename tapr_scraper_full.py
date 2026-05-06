import requests
import os
import time
from pathlib import Path
import re
import html

class TaprScraper:
    BASE_URL = "https://rptsvr1.tea.texas.gov/cgi/sas/broker"
    
    LEVELS = {
        "all_c": "Campuses",
        "all_d": "Districts",
        "all_r": "Regions",
        "all_co": "Counties",
        "all_s": "State"
    }
    
    def __init__(self, output_dir="tapr_data"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.session = requests.Session()
        # Set a reasonable user agent
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })

    def get_categories(self, year, level):
        """Step 3: Fetch the categories available for a given year and level."""
        params = {
            "_service": "marykay",
            "_program": "perfrept.perfmast.sas",
            "_debug": "0",
            "ccyy": year,
            "tapr": level,
            "prgopt": "reports/tapr/dd/dd_tapr.sas"
        }
        
        try:
            response = self.session.get(self.BASE_URL, params=params)
            response.raise_for_status()
            text = response.text
            
            # Extract radio buttons named 'dsname' using regex
            # <input type='radio' name='dsname' value='REF' id='dd1' checked>
            radio_pattern = re.compile(r"<input[^>]+name=['\"]dsname['\"][^>]+value=['\"]([^'\"]+)['\"][^>]+id=['\"]([^'\"]+)['\"]", re.IGNORECASE)
            radios = radio_pattern.findall(text)
            
            categories = []
            for val, r_id in radios:
                # Find label for this id: <label for='dd1'>Campus Reference<label>
                label_pattern = re.compile(rf"<label[^>]+for=['\"]{r_id}['\"][^>]*>([^<]+)", re.IGNORECASE)
                label_match = label_pattern.search(text)
                label_text = label_match.group(1).strip() if label_match else val
                categories.append({'value': val, 'label': html.unescape(label_text)})
            
            # Extract hidden fields
            # <input type='hidden' name='_service' value=marykay>
            hidden_pattern = re.compile(r"<input[^>]+type=['\"]hidden['\"][^>]+name=['\"]([^'\"]+)['\"][^>]+value=['\"]([^'\"]*)['\"]", re.IGNORECASE)
            hidden_fields = dict(hidden_pattern.findall(text))
            
            # Handle unquoted values if any (like value=marykay)
            unquoted_hidden_pattern = re.compile(r"<input[^>]+type=['\"]hidden['\"][^>]+name=['\"]([^'\"]+)['\"][^>]+value=([^'\" >]+)", re.IGNORECASE)
            hidden_fields.update(dict(unquoted_hidden_pattern.findall(text)))
                
            return categories, hidden_fields
        except Exception as e:
            print(f"Error getting categories for {year} {level}: {e}")
            return [], {}

    def get_elements_and_download(self, year, level, category_info, step3_hidden):
        """Step 4: Select all elements and download the CSV."""
        data = step3_hidden.copy()
        data['dsname'] = category_info['value']
        data['step'] = '3'
        
        try:
            response = self.session.post(self.BASE_URL, data=data)
            response.raise_for_status()
            text = response.text
            
            # Extract checkboxes named 'key'
            # <input type="checkbox" class="ddlist" name="key" id="key1" value="C_RATING" checked>
            key_pattern = re.compile(r"<input[^>]+name=['\"]key['\"][^>]+value=['\"]([^'\"]+)['\"]", re.IGNORECASE)
            keys = key_pattern.findall(text)
                
            if not keys:
                print(f"No keys found for {year} {level} {category_info['label']}")
                return
            
            # Extract hidden fields for the final step
            hidden_pattern = re.compile(r"<input[^>]+type=['\"]hidden['\"][^>]+name=['\"]([^'\"]+)['\"][^>]+value=['\"]([^'\"]*)['\"]", re.IGNORECASE)
            final_data = dict(hidden_pattern.findall(text))
            
            unquoted_hidden_pattern = re.compile(r"<input[^>]+type=['\"]hidden['\"][^>]+name=['\"]([^'\"]+)['\"][^>]+value=([^'\" >]+)", re.IGNORECASE)
            final_data.update(dict(unquoted_hidden_pattern.findall(text)))
            
            # Final parameters for download
            # Using list for 'key' to send multiple values
            post_data = []
            for k, v in final_data.items():
                post_data.append((k, v))
            for key in keys:
                post_data.append(('key', key))
            post_data.append(('datafmt', 'csv'))
            
            # Final POST request for download
            download_url = self.BASE_URL + "/"
            response = self.session.post(download_url, data=post_data, stream=True)
            response.raise_for_status()
            
            # Determine filename
            content_disposition = response.headers.get('Content-Disposition')
            if content_disposition:
                filename_match = re.search(r"filename=(.+)", content_disposition)
                if filename_match:
                    filename = filename_match.group(1).strip('"')
                else:
                    filename = f"{year}_{level}_{category_info['value']}.csv"
            else:
                filename = f"{year}_{level}_{category_info['value']}.csv"
                
            # Clean filename
            filename = "".join([c for c in filename if c.isalnum() or c in (' ', '.', '_', '-')]).strip()
            
            # Save file
            year_dir = self.output_dir / str(year)
            level_dir = year_dir / self.LEVELS.get(level, level)
            level_dir.mkdir(parents=True, exist_ok=True)
            
            file_path = level_dir / filename
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
                    
            print(f"Downloaded: {file_path}")
            
        except Exception as e:
            print(f"Error downloading {year} {level} {category_info['label']}: {e}")

    def run(self, years, levels=None, category_limit=None):
        if levels is None:
            levels = list(self.LEVELS.keys())
            
        total_downloaded = 0
        for year in years:
            for level in levels:
                print(f"Processing Year: {year}, Level: {self.LEVELS.get(level, level)}")
                categories, hidden_fields = self.get_categories(year, level)
                
                if not categories:
                    print(f"No categories found for {year} {level}")
                    continue
                    
                count = 0
                for cat in categories:
                    print(f"  Category: {cat['label']}")
                    self.get_elements_and_download(year, level, cat, hidden_fields)
                    total_downloaded += 1
                    count += 1
                    if category_limit and count >= category_limit:
                        break
                    # Politeness delay to avoid overwhelming the server
                    time.sleep(1)
        
        print(f"\nFinished! Total files downloaded: {total_downloaded}")

if __name__ == "__main__":
    # TAPR data is available from 2013 (2012-13 school year) onwards.
    # The system currently supports years up to 2025.
    available_years = range(2013, 2026) 
    
    # Geographic levels to download
    # all_c: Campuses, all_d: Districts, all_r: Regions, all_co: Counties, all_s: State
    available_levels = ["all_c", "all_d", "all_r", "all_co", "all_s"]
    
    scraper = TaprScraper(output_dir="tapr_data")
    
    print("TEA TAPR Data Downloader")
    print("------------------------")
    print(f"Years: {available_years.start} to {available_years.stop - 1}")
    print(f"Levels: {', '.join([scraper.LEVELS[l] for l in available_levels])}")
    print("Starting download... (This may take a long time)\n")
    
    # By default, we run for all years and all levels.
    # You can restrict this for testing, e.g., years=[2024], levels=['all_s']
    scraper.run(years=available_years, levels=available_levels)
