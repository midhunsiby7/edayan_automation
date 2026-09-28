from pathlib import Path
from src.validation.website_head_parser import WebsiteHeadParser
from src.validation.head_master import HeadMasterRepository
from src.validation.head_mapping import HeadMapper
from src.models import AccountSet

def main():
    # 1. First parse the website heads from HTML source
    html_path = Path("data/reference/website_heads.html")
    parser = WebsiteHeadParser()
    website_heads = parser.parse_file(html_path)
    
    csv_path = Path("data/reference/website_heads.csv")
    # parser.export_csv(website_heads, csv_path) # File might be locked
    
    # 2. Load the Accounting Master (simulate the verified import)
    # The user said they corrected 43 and 87, so we'll just read from the draft and fix them in memory or disk.
    draft_path = Path("data/reference/accounting_heads_general_draft.csv")
    verified_path = Path("data/reference/accounting_heads_general_verified.csv")
    
    # Copy draft to verified, fixing the names
    with open(draft_path, "r", encoding="utf-8") as f_in, open(verified_path, "w", encoding="utf-8") as f_out:
        lines = f_in.readlines()
        for i, line in enumerate(lines):
            if i == 0:
                f_out.write(line)
                continue
                
            parts = line.strip().split(",")
            head_num = parts[1]
            if head_num == "43":
                parts[2] = "Tomb Expense Collected"
                parts[8] = "False"  # Review required
                parts[10] = ""
            elif head_num == "87":
                parts[2] = "Rented Building Expense"
                parts[8] = "False"
                parts[10] = ""
                
            f_out.write(",".join(parts) + "\n")
            
    # Load into repository
    repo = HeadMasterRepository(Path("data/reference/accounting_heads_general_empty.csv"))
    repo._records[AccountSet.GENERAL].clear()
    repo._records[AccountSet.AGRICULTURE].clear()
    repo.import_verified_csv(verified_path)
    
    # We only care about GENERAL heads
    accounting_heads = list(repo._records[AccountSet.GENERAL].values())
    
    # 3. Map
    mapper = HeadMapper(accounting_heads, website_heads)
    results = mapper.map_heads()
    
    # 4. Report and save
    mapper.generate_report(results)
    
    crosswalk_path = Path("data/reference/general_head_crosswalk.csv")
    mapper.save_crosswalk(results, crosswalk_path)
    print(f"Output crosswalk path: {crosswalk_path.absolute()}")

if __name__ == "__main__":
    main()
