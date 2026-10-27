import os
import shutil
from pathlib import Path
from datetime import datetime

def main():
    base_dir = Path("data")
    ref_dir = base_dir / "reference"
    
    # 1. Create directories
    active_dir = ref_dir / "active"
    backups_dir = ref_dir / "backups"
    archive_dir = ref_dir / "archive"
    monthly_dir = base_dir / "monthly"
    
    for d in [active_dir, backups_dir, archive_dir, monthly_dir]:
        d.mkdir(parents=True, exist_ok=True)
        
    # 2. Move verified master to active and create backup
    verified_csv = ref_dir / "accounting_heads_general_verified.csv"
    if verified_csv.exists():
        active_csv = active_dir / "accounting_heads_general_verified.csv"
        shutil.move(str(verified_csv), str(active_csv))
        print(f"Moved {verified_csv.name} to {active_dir}")
        
        # Backup
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_csv = backups_dir / f"accounting_heads_general_verified_{timestamp}.csv"
        shutil.copy2(str(active_csv), str(backup_csv))
        print(f"Created backup at {backup_csv}")
        
    # 3. Archive old artifacts
    artifacts = [
        "accounting_heads_general.csv",
        "accounting_heads_general_draft.csv",
        "accounting_heads_general_empty.csv",
        "accounting_heads_general_review.xlsx",
        "accounting_heads_general_human_review.xlsx",
        "general_head_crosswalk.csv",
        "general_head_mapping_review.xlsx"
    ]
    for artifact in artifacts:
        p = ref_dir / artifact
        if p.exists():
            shutil.move(str(p), str(archive_dir / artifact))
            print(f"Archived {artifact}")

if __name__ == "__main__":
    main()
