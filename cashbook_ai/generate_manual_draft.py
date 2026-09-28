import csv
from pathlib import Path
import openpyxl

# Manual transcription from images
# Fields: number, name, image, row_in_img, review_required, warning
data = [
    # Image 1 (Income)
    (1, "Opening Balance", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 1, False, ""),
    (2, "Adima, kazhunnu", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 2, False, ""),
    (3, "Advance From Agriculture Account", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 3, False, ""),
    (4, "Bank F.D. withdrawal", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 4, False, ""),
    (5, "Bank Interest", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 5, False, ""),
    (6, "Bank S.B. withdrawal", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 6, False, ""),
    (10, "Candle offerings", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 7, False, ""),
    (11, "Charge From church Articles", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 8, False, ""),
    (13, "Church Feast Collection", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 9, False, ""),
    (14, "Collection As Per the Order From the Diocese", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 10, False, ""),
    (16, "Default Fee Received", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 11, False, ""),
    (17, "Donation From Diocese for charity works", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 12, False, ""),
    (18, "Funeral offerings", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 13, False, ""),
    (19, "Marriage offerings", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 14, False, ""),
    (20, "Offerings For Daily Expenses", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 15, False, ""),
    (24, "Offering For Special Purpose", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 16, False, ""),
    (28, "Offerings Auction, Bhandaram E.T.C. (Nercha)", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 17, False, ""),
    (33, "Rent From Building", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 18, False, ""),
    (35, "Rent Security", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 19, False, ""),
    (36, "Sale of offered Materials (Silver, gold)", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 20, False, ""),
    (37, "Sale of old things", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 21, False, ""),
    (38, "Sales Stall Income", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 22, False, ""),
    (39, "School Income", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 23, False, ""),
    (40, "Sunday Collection (Vedapadhasam) Income", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 24, False, ""),
    (43, "Tomb Expense Collected", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 25, True, "Handwriting is unclear, looks like 'Tomb Expense Collected'"),
    (44, "Tomb Fund Collected", "WhatsApp Image 2026-09-28 at 7.30.28 PM.jpeg", 26, False, ""),
    
    # Image 2 (Expenditure)
    (51, "Advance to Agriculture Account", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 1, False, ""),
    (52, "Allowances: Rev. Fr. Vicar", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 2, False, ""),
    (53, "Allowances: Accountant", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 3, False, ""),
    (54, "Allowances: Computer Assistant", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 4, False, ""),
    (55, "Allowances: Sacristan", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 5, False, ""),
    (56, "Allowances: Sweeper", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 6, False, ""),
    (57, "Bank Charges", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 7, False, ""),
    (58, "Bank F.D. Deposit", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 8, False, ""),
    (59, "Bank S.B. Deposit", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 9, False, ""),
    (61, "Cathidraticum Fees to the Diocese", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 10, False, ""),
    (62, "Church Articles & Vestments", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 11, False, ""),
    (64, "Church Feast Expense", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 12, False, ""),
    (66, "Collection Paid to the Diocese", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 13, False, ""),
    (67, "Default Fee Paid", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 14, False, ""),
    (68, "Donation from the Diocese Disbursed", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 15, False, ""),
    (69, "Electricity Charges", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 16, False, ""),
    (70, "Furniture", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 17, False, ""),
    (71, "Legal & Audit fee", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 18, False, ""),
    (72, "License And tax", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 19, False, ""),
    (73, "Machinery & Equipment", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 20, False, ""),
    (74, "Maintenance & Repair", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 21, False, ""),
    (78, "Miscellaneous Expenses", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 22, False, ""),
    (82, "New Construction", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 23, False, ""),
    (84, "Passaram Share to ADCP, Accountant, Sacristan", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 24, False, ""),
    (85, "Postage & Telephone", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 25, False, ""),
    (86, "Printing & Stationary", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 26, False, ""),
    (87, "Rental Building Expense", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 27, True, "Handwriting unclear, looks like Rental Building Expense or Rent & Building Expense"),
    (88, "Rent Security Returned", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 28, False, ""),
    (89, "School Expense", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 29, False, ""),
    (90, "Social work (charity)", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 30, False, ""),
    (92, "Sunday Collection to the Diocese", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 31, False, ""),
    (93, "Sunday School Expense", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 32, False, ""),
    (97, "Travelling Expense", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 33, False, ""),
    (99, "Wine, Host, Candle, Incense E.T.C.", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 34, False, ""),
    (101, "A.D.C.P, N.C.F, P.W.F.", "WhatsApp Image 2026-09-28 at 7.31.00 PM.jpeg", 35, False, ""),
]

csv_path = Path("data/reference/accounting_heads_general_draft.csv")
excel_path = Path("data/reference/accounting_heads_general_review.xlsx")

# 1. Generate CSV
with open(csv_path, "w", newline="", encoding="utf-8") as f:
    writer = csv.writer(f)
    # Required schema
    writer.writerow([
        "account_set", "accounting_head_number", "accounting_head_name",
        "normalized_accounting_head_name", "source_image", "source_page", "source_row",
        "extraction_confidence", "review_required", "source_type", "warning"
    ])
    
    for num, name, img, row, rev, warning in data:
        conf = 0.80 if rev else 0.99
        writer.writerow([
            "GENERAL", str(num), name, "", img, img, str(row),
            f"{conf:.2f}", str(rev), "MANUAL_VERIFIED_PENDING", warning
        ])

# 2. Generate Excel for Review
wb = openpyxl.Workbook()
ws = wb.active
ws.title = "Review"

headers = ["Head Number", "Extracted Head Name", "Source Image", "Source Row", "Confidence", "Review Required", "Warning"]
ws.append(headers)

for num, name, img, row, rev, warning in data:
    conf = 0.80 if rev else 0.99
    ws.append([num, name, img, row, conf, rev, warning])

wb.save(excel_path)

print(f"Generated Draft CSV: {csv_path.absolute()}")
print(f"Generated Review Excel: {excel_path.absolute()}")
print(f"Total entries found: {len(data)}")
