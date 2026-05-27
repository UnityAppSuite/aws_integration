import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


def after_migrate():
    custom_fields = {
        "File": [
            {
                "fieldname": "s3_info_section",
                "fieldtype": "Section Break",
                "label": "S3 Info",
                "insert_after": "preview",
                "collapsible": 1,
            },
            {
                "fieldname": "s3_key",
                "fieldtype": "Small Text",
                "label": "S3 Key",
                "insert_after": "s3_info_section",
                "read_only": 1,
                "no_copy": 1,
            },
            # Row 2: status fields in two columns
            {
                "fieldname": "s3_status_section",
                "fieldtype": "Section Break",
                "insert_after": "s3_key",
                "hide_border": 1,
            },
            {
                "fieldname": "is_on_s3",
                "fieldtype": "Check",
                "label": "Uploaded to S3",
                "insert_after": "s3_status_section",
                "read_only": 1,
                "default": "0",
                "no_copy": 1,
            },
            {
                "fieldname": "s3_uploaded_at",
                "fieldtype": "Datetime",
                "label": "S3 Upload Date",
                "insert_after": "is_on_s3",
                "read_only": 1,
                "no_copy": 1,
            },
            {
                "fieldname": "s3_col_break",
                "fieldtype": "Column Break",
                "insert_after": "s3_uploaded_at",
            },
            {
                "fieldname": "local_deleted",
                "fieldtype": "Check",
                "label": "Local File Deleted",
                "insert_after": "s3_col_break",
                "read_only": 1,
                "default": "0",
                "no_copy": 1,
            },
            {
                "fieldname": "s3_upload_skipped",
                "fieldtype": "Check",
                "label": "S3 Upload Skipped (File Missing)",
                "insert_after": "local_deleted",
                "read_only": 1,
                "default": "0",
                "no_copy": 1,
            },
        ]
    }
    # Table definition may have changed earlier in migrate; retry once to pick up the fresh schema.
    try:
        create_custom_fields(custom_fields, update=True)
    except Exception:
        frappe.db.rollback()
        create_custom_fields(custom_fields, update=True)
        frappe.db.commit()
