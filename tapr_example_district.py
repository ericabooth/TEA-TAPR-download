import requests
import os
import time
from pathlib import Path
import re
import html

class TaprExampleScraper:
    """
    Example scraper that downloads exactly one category for All Districts for a specific year.
    """
    BASE_URL = "https://rptsvr1.tea.texas.gov/cgi/sas/broker"
    
    def __init__(self, output_dir="tapr_example"):
        self.output_dir = Path(output_dir)
        self.output_dir.mkdir(exist_ok=True)
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        })

    def download_district_reference(self, year):
        print(f"Starting example download for Year: {year}, Level: Districts, Category: District Reference")
        
        # Step 3: Get the categories and hidden fields for Districts (all_d)
        params = {
            "_service": "marykay",
            "_program": "perfrept.perfmast.sas",
            "_debug": "0",
            "ccyy": year,
            "tapr": "all_d",
            "prgopt": "reports/tapr/dd/dd_tapr.sas"
        }
        
        try:
            response = self.session.get(self.BASE_URL, params=params)
            response.raise_for_status()
            text = response.text
            
            # Extract hidden fields
            hidden_pattern = re.compile(r"<input[^>]+type=['\"]hidden['\"][^>]+name=['\"]([^'\"]+)['\"][^>]+value=['\"]([^'\"]*)['\"]", re.IGNORECASE)
            hidden_fields = dict(hidden_pattern.findall(text))
            unquoted_hidden_pattern = re.compile(r"<input[^>]+type=['\"]hidden['\"][^>]+name=['\"]([^'\"]+)['\"][^>]+value=([^'\" >]+)", re.IGNORECASE)
            hidden_fields.update(dict(unquoted_hidden_pattern.findall(text)))

            # Step 4: Request the District Reference category (dsname=REF)
            data = hidden_fields.copy()
            data['dsname'] = 'REF'
            data['step'] = '3'
            
            response = self.session.post(self.BASE_URL, data=data)
            response.raise_for_status()
            text = response.text
            
            # Extract all data element checkboxes ('key')
            key_pattern = re.compile(r"<input[^>]+name=['\"]key['\"][^>]+value=['\"]([^'\"]+)['\"]", re.IGNORECASE)
            keys = key_pattern.findall(text)
            
            # Extract hidden fields for the final step
            final_hidden = dict(hidden_pattern.findall(text))
            final_hidden.update(dict(unquoted_hidden_pattern.findall(text)))
            
            # Final Step: Construct the download request
            post_data = []
            for k, v in final_hidden.items():
                post_data.append((k, v))
            for key in keys:
                post_data.append(('key', key))
            post_data.append(('datafmt', 'csv'))
            
            print(f"  Requesting download for {len(keys)} data elements...")
            response = self.session.post(self.BASE_URL + "/", data=post_data, stream=True)
            response.raise_for_status()
            
            # Save the file
            filename = f"tapr_{year}_district_reference.csv"
            file_path = self.output_dir / filename
            with open(file_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            print(f"Success! File saved to: {file_path}")
            
        except Exception as e:
            print(f"Error during download: {e}")

if __name__ == "__main__":
    scraper = TaprExampleScraper()
    # Download 2024 District Reference as an example
    scraper.download_district_reference(year=2024)
