import datetime
import re
from pathlib import Path
from typing import Any, Iterable, List, Optional

import openpyxl
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.workbook.defined_name import DefinedName
from openpyxl.worksheet.datavalidation import DataValidation

from src.models import ProcessedRow, RecordType, TransactionType


MANUAL_REVIEW_COLUMNS = [
    "Date",
    "Receipt Number",
    "Amount",
    "Head Name",
    "Subhead",
    "Narration",
]

REVIEW_SHEET_COLUMNS = [
    "Date",
    "Voucher Number",
    "Folio",
    "Amount",
    "Head Name",
    "Subhead",
    "Narration",
]

ALL_TRANSACTIONS_COLUMNS = [
    "Date",
    "Voucher Number",
    "Folio",
    "Direction",
    "Amount",
    "Head Name",
    "Subhead",
    "Narration",
]


def parse_date_value(date_val: Any) -> Optional[datetime.date]:
    """Parse various date representations (ISO, dot, slash, hyphen) into a datetime.date object."""
    if not date_val:
        return None
    if isinstance(date_val, datetime.datetime):
        return date_val.date()
    if isinstance(date_val, datetime.date):
        return date_val
    if not isinstance(date_val, str):
        return None
    cleaned = date_val.strip()
    if not cleaned or cleaned.lower() in ("none", "null", "-", "—"):
        return None
    cleaned_date = cleaned.split()[0].split("T")[0]
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", cleaned_date):
            parts = [int(p) for p in cleaned_date.split("-")]
            return datetime.date(parts[0], parts[1], parts[2])
    except Exception:
        pass
    try:
        m = re.match(r"^(\d{1,2})[\.\/\-](\d{1,2})[\.\/\-](\d{2,4})$", cleaned_date)
        if m:
            d, m_val, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
            if y < 100:
                y += 2000
            return datetime.date(y, m_val, d)
    except Exception:
        pass
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%d.%m.%y", "%d/%m/%y", "%d-%m-%y"):
        try:
            return datetime.datetime.strptime(cleaned_date, fmt).date()
        except Exception:
            continue
    return None


class ManualReviewExporter:
    """Exports transaction rows to the standardized, high-readability monthly review workbook."""

    def export(
        self,
        rows: Iterable[ProcessedRow],
        output_path: Path,
        *,
        include_all_transactions: bool = False,
        include_folio: bool = False,
        head_name_overrides: dict[tuple[str, str, int], str | None] | None = None,
    ) -> Path:
        rows = list(rows)
        workbook = openpyxl.Workbook()
        workbook.remove(workbook.active)

        receipts = [
            row for row in rows
            if row.transaction_type == TransactionType.RECEIPT
            and row.record_type == RecordType.TRANSACTION
        ]
        payments = [
            row for row in rows
            if row.transaction_type == TransactionType.PAYMENT
            and row.record_type == RecordType.TRANSACTION
        ]

        self._write_sheet(
            workbook, "Receipts", receipts, "Receipt",
            include_folio=include_folio,
            head_name_overrides=head_name_overrides,
        )
        self._write_sheet(
            workbook, "Payments", payments, "Payment",
            include_folio=include_folio,
            head_name_overrides=head_name_overrides,
        )

        if include_all_transactions:
            all_txs = [
                row for row in rows
                if row.record_type == RecordType.TRANSACTION
                and row.transaction_type in (TransactionType.RECEIPT, TransactionType.PAYMENT)
            ]
            self._write_sheet(
                workbook, "All_Transactions", all_txs, "All",
                include_folio=include_folio,
                is_all_sheet=True,
                head_name_overrides=head_name_overrides,
            )

        output_path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(output_path)
        return output_path

    def export_review_workbook(
        self,
        rows: Iterable[ProcessedRow],
        output_path: Path,
        *,
        head_name_overrides: dict[tuple[str, str, int], str | None] | None = None,
    ) -> Path:
        """Export user-facing review workbook with Receipts, Payments, and All_Transactions sheets."""
        return self.export(
            rows,
            output_path,
            include_all_transactions=True,
            include_folio=True,
            head_name_overrides=head_name_overrides,
        )

    def export_with_directions(
        self,
        rows: Iterable[ProcessedRow],
        directions: dict[tuple[str, str, int], TransactionType],
        output_path: Path,
        *,
        canonical_head_names: Iterable[str] | None = None,
        head_name_overrides: dict[tuple[str, str, int], str | None] | None = None,
        include_all_transactions: bool = False,
        include_folio: bool = False,
    ) -> Path:
        """Export using explicit human-approved directions without mutating processed rows."""
        rows = list(rows)
        head_name_overrides = head_name_overrides or {}
        receipts = [
            row for row in rows
            if row.record_type == RecordType.TRANSACTION
            and directions.get((row.source_image, row.source_page, row.source_row)) == TransactionType.RECEIPT
        ]
        payments = [
            row for row in rows
            if row.record_type == RecordType.TRANSACTION
            and directions.get((row.source_image, row.source_page, row.source_row)) == TransactionType.PAYMENT
        ]

        workbook = Workbook()
        workbook.remove(workbook.active)
        self._write_sheet(
            workbook,
            "Receipts",
            receipts,
            "Receipt",
            include_folio=include_folio,
            head_name_overrides=head_name_overrides,
        )
        self._write_sheet(
            workbook,
            "Payments",
            payments,
            "Payment",
            include_folio=include_folio,
            head_name_overrides=head_name_overrides,
        )

        if include_all_transactions:
            all_txs = [
                row for row in rows
                if row.record_type == RecordType.TRANSACTION
                and directions.get((row.source_image, row.source_page, row.source_row)) in (
                    TransactionType.RECEIPT,
                    TransactionType.PAYMENT,
                )
            ]
            self._write_sheet(
                workbook,
                "All_Transactions",
                all_txs,
                "All",
                include_folio=include_folio,
                is_all_sheet=True,
                directions=directions,
                head_name_overrides=head_name_overrides,
            )

        if canonical_head_names is not None:
            self._add_head_name_validation(workbook, list(canonical_head_names))
        output_path.parent.mkdir(parents=True, exist_ok=True)
        workbook.save(output_path)
        return output_path

    def _write_sheet(
        self,
        workbook: openpyxl.Workbook,
        title: str,
        rows: List[ProcessedRow],
        amount_label: str,
        *,
        include_folio: bool = False,
        is_all_sheet: bool = False,
        directions: dict[tuple[str, str, int], TransactionType] | None = None,
        head_name_overrides: dict[tuple[str, str, int], str | None] | None = None,
    ) -> None:
        worksheet = workbook.create_sheet(title)

        # Determine column structure
        if is_all_sheet:
            headers = ALL_TRANSACTIONS_COLUMNS if include_folio else [
                "Date", "Voucher Number", "Direction", "Amount", "Head Name", "Subhead", "Narration"
            ]
        elif include_folio:
            headers = REVIEW_SHEET_COLUMNS
        else:
            headers = MANUAL_REVIEW_COLUMNS

        worksheet.append(headers)

        # Styles
        header_fill = PatternFill("solid", fgColor="1F4E79")
        header_font = Font(name="Calibri", size=12, bold=True, color="FFFFFF")
        header_align = Alignment(horizontal="center", vertical="center")

        thin_side = Side(border_style="thin", color="E2E8F0")
        cell_border = Border(left=thin_side, right=thin_side, top=thin_side, bottom=thin_side)

        key_font = Font(name="Calibri", size=11, bold=True, color="1E293B")
        sub_font = Font(name="Calibri", size=10, bold=False, color="475569")

        zebra_fill_white = PatternFill("solid", fgColor="FFFFFF")
        zebra_fill_tint = PatternFill("solid", fgColor="F8FAFC")
        warn_fill = PatternFill("solid", fgColor="FFF3CD")

        receipt_dir_font = Font(name="Calibri", size=11, bold=True, color="065F46")
        receipt_dir_fill = PatternFill("solid", fgColor="D1FAE5")
        payment_dir_font = Font(name="Calibri", size=11, bold=True, color="991B1B")
        payment_dir_fill = PatternFill("solid", fgColor="FEE2E2")

        # Format header row
        worksheet.row_dimensions[1].height = 28
        for cell in worksheet[1]:
            cell.font = header_font
            cell.fill = header_fill
            cell.alignment = header_align
            cell.border = cell_border

        head_name_overrides = head_name_overrides or {}
        directions = directions or {}

        for row_idx, row in enumerate(rows, start=2):
            worksheet.row_dimensions[row_idx].height = 24
            row_fill = zebra_fill_white if (row_idx % 2 == 0) else zebra_fill_tint

            key = (row.source_image, row.source_page, row.source_row)
            dir_override = directions.get(key)
            eff_dir = dir_override or row.transaction_type

            # Resolve amount
            if is_all_sheet:
                if eff_dir == TransactionType.RECEIPT:
                    amount = row.receipt_amount if row.receipt_amount is not None else row.payment_amount
                else:
                    amount = row.payment_amount if row.payment_amount is not None else row.receipt_amount
            elif amount_label == "Receipt":
                amount = row.receipt_amount if row.receipt_amount is not None else row.payment_amount
            else:
                amount = row.payment_amount if row.payment_amount is not None else row.receipt_amount

            if amount is not None:
                try:
                    amount = float(amount)
                except (ValueError, TypeError):
                    amount = 0.0

            voucher_number = str(row.raw_voucher_number or row.voucher_number or "").strip()
            folio_val = str(row.raw_head_number or "").strip()
            head_name = head_name_overrides.get(key, row.accounting_head_name)
            subhead_val = str(row.subhead or "").strip()
            narration_val = str(row.narration_english or row.raw_narration or "").strip()

            # Resolve native Excel date
            date_raw = row.resolved_date or row.raw_date
            parsed_date = parse_date_value(date_raw)

            # Build row cells
            row_cells = []
            if is_all_sheet:
                dir_str = "RECEIPT" if eff_dir == TransactionType.RECEIPT else "PAYMENT"
                if include_folio:
                    row_cells = [
                        (parsed_date if parsed_date else (date_raw or ""), "date"),
                        (voucher_number, "voucher"),
                        (folio_val, "folio"),
                        (dir_str, "direction"),
                        (amount, "amount"),
                        (head_name or "", "head"),
                        (subhead_val, "subhead"),
                        (narration_val, "narration"),
                    ]
                else:
                    row_cells = [
                        (parsed_date if parsed_date else (date_raw or ""), "date"),
                        (voucher_number, "voucher"),
                        (dir_str, "direction"),
                        (amount, "amount"),
                        (head_name or "", "head"),
                        (subhead_val, "subhead"),
                        (narration_val, "narration"),
                    ]
            elif include_folio:
                row_cells = [
                    (parsed_date if parsed_date else (date_raw or ""), "date"),
                    (voucher_number, "voucher"),
                    (folio_val, "folio"),
                    (amount, "amount"),
                    (head_name or "", "head"),
                    (subhead_val, "subhead"),
                    (narration_val, "narration"),
                ]
            else:
                row_cells = [
                    (parsed_date if parsed_date else (date_raw or ""), "date"),
                    (voucher_number, "voucher"),
                    (amount, "amount"),
                    (head_name or "", "head"),
                    (subhead_val, "subhead"),
                    (narration_val, "narration"),
                ]

            for col_idx, (val, cell_type) in enumerate(row_cells, start=1):
                cell = worksheet.cell(row=row_idx, column=col_idx, value=val)
                cell.border = cell_border
                cell.fill = row_fill

                if cell_type == "date":
                    if isinstance(val, (datetime.date, datetime.datetime)):
                        cell.number_format = "DD-MM-YYYY"
                    cell.font = key_font
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                elif cell_type == "voucher":
                    cell.number_format = "@"
                    cell.font = key_font
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                elif cell_type == "folio":
                    cell.number_format = "@"
                    cell.font = sub_font
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                elif cell_type == "direction":
                    cell.alignment = Alignment(horizontal="center", vertical="center")
                    if val == "RECEIPT":
                        cell.font = receipt_dir_font
                        cell.fill = receipt_dir_fill
                    else:
                        cell.font = payment_dir_font
                        cell.fill = payment_dir_fill
                elif cell_type == "amount":
                    cell.number_format = "#,##0.00"
                    cell.font = key_font
                    cell.alignment = Alignment(horizontal="right", vertical="center")
                elif cell_type == "head":
                    cell.font = key_font
                    cell.alignment = Alignment(horizontal="left", vertical="center")
                    if not val:
                        cell.fill = warn_fill
                elif cell_type == "subhead":
                    cell.font = sub_font
                    cell.alignment = Alignment(horizontal="left", vertical="center")
                elif cell_type == "narration":
                    cell.font = key_font
                    cell.alignment = Alignment(horizontal="left", vertical="center", wrap_text=True)

        # Set column widths
        widths_map = {
            "Date": 16,
            "Receipt Number": 18,
            "Voucher Number": 18,
            "Folio": 10,
            "Direction": 16,
            "Amount": 18,
            "Head Name": 45,
            "Subhead": 26,
            "Narration": 55,
        }
        for index, col_name in enumerate(headers, start=1):
            col_letter = openpyxl.utils.get_column_letter(index)
            worksheet.column_dimensions[col_letter].width = widths_map.get(col_name, 20)

        worksheet.freeze_panes = "A2"
        if worksheet.max_row >= 1:
            worksheet.auto_filter.ref = worksheet.dimensions

    def _add_head_name_validation(self, workbook: Workbook, canonical_names: List[str]) -> None:
        unique_names = list(dict.fromkeys(name.strip() for name in canonical_names if name and name.strip()))
        helper = workbook.create_sheet("__HeadNames")
        helper.sheet_state = "hidden"
        helper["A1"] = "Canonical human_final_name"
        for index, name in enumerate(unique_names, start=2):
            helper.cell(index, 1).value = name
        end_row = max(2, len(unique_names) + 1)
        workbook.defined_names.add(
            DefinedName(
                "CanonicalHeadNames",
                attr_text=f"'__HeadNames'!$A$2:$A${end_row}",
            )
        )
        validation = DataValidation(
            type="list",
            formula1="=CanonicalHeadNames",
            allow_blank=True,
        )
        validation.error = "Select a canonical head name from the verified master."
        validation.errorTitle = "Invalid head name"
        validation.prompt = "Choose a canonical human-verified head name."
        validation.promptTitle = "Head Name"
        for sheet_name in ("Receipts", "Payments"):
            if sheet_name in workbook.sheetnames:
                sheet = workbook[sheet_name]
                sheet.add_data_validation(validation)
                validation.add(f"D2:D{max(sheet.max_row, 200)}")
