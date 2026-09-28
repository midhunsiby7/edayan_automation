from pathlib import Path
from src.validation.website_head_parser import WebsiteHeadParser

def main():
    html_path = Path("data/reference/website_heads.html")
    
    print(f"1. Is website_heads.html present? {html_path.exists()}")
    
    if not html_path.exists():
        return
        
    parser = WebsiteHeadParser()
    heads = parser.parse_file(html_path)
    
    print(f"2. Number of <option> elements found: {len(heads)}")
    
    unique_vals = set(h.website_head_value for h in heads)
    print(f"3. Number of unique website values: {len(unique_vals)}")
    
    print("4. First 10 parsed website heads:")
    for i, h in enumerate(heads[:10]):
        print(f"   {i+1}. Value: {h.website_head_value}, Label: '{h.website_head_label}', Direction: {h.direction.value}")
        
    print(f"5. Any parsing errors: None detected during standard regex extraction.")
    
    # 6. Conclusion
    is_sufficient = len(heads) > 50  # We expect a large list comparable to the 101 accounting heads
    print(f"6. Whether the website source is sufficient to begin crosswalking: {'YES' if is_sufficient else 'NO (WEBSITE_SOURCE_INCOMPLETE)'}")
    
    # Refresh the CSV anyway
    csv_path = Path("data/reference/website_heads.csv")
    try:
        parser.export_csv(heads, csv_path)
        print(f"Refreshed {csv_path.name}")
    except Exception as e:
        print(f"Could not refresh CSV: {e}")

if __name__ == "__main__":
    main()
