frappe.ui.form.on("File", {
    refresh: function (frm) {
        // With S3 switched off the form says nothing about S3 (see s3_ui_visible).
        if (!s3_ui_visible(frm)) {
            set_s3_fields_hidden(frm, true);
            return;
        }
        // A form is reused from file to file, so undo what an earlier file hid.
        set_s3_fields_hidden(frm, false);

        if (frm.doc.is_on_s3 && frm.doc.s3_key) {
            // Add S3 indicator
            let label = frm.doc.local_deleted
                ? __("Stored on S3 (local deleted)")
                : __("Stored on S3");
            frm.dashboard.set_headline(
                `<span class="indicator-pill green">${label}</span>`
            );

            // Show "View on S3" button when local copy still exists (file_url points to local)
            if (!frm.doc.local_deleted) {
                frm.add_custom_button(__("View on S3"), function () {
                    frappe.call({
                        method: "aws_integration.api.s3.get_file_preview",
                        args: { file_name: frm.doc.name },
                        callback: function (r) {
                            if (r.message && r.message.url) {
                                window.open(r.message.url, "_blank");
                            }
                        },
                    });
                });
            }

            // Show "Delete Local File" button if file is on S3 but local copy exists
            if (!frm.doc.local_deleted && frappe.user.has_role("System Manager")) {
                frm.add_custom_button(__("Delete Local File"), function () {
                    frappe.confirm(
                        __("This will permanently delete the local copy of this file. The file will still be available on S3. Continue?"),
                        function () {
                            frappe.call({
                                method: "aws_integration.api.s3.delete_local_file",
                                args: { file_name: frm.doc.name },
                                freeze: true,
                                freeze_message: __("Deleting local file..."),
                                callback: function (r) {
                                    if (r.message && r.message.success) {
                                        frappe.show_alert({
                                            message: __("Local file deleted successfully."),
                                            indicator: "green",
                                        }, 5);
                                        frm.reload_doc();
                                    }
                                },
                            });
                        }
                    );
                });
            }
        } else if (!frm.doc.is_on_s3 && frm.doc.file_url && frm.doc.file_url.startsWith("/")) {
            frm.add_custom_button(__("Upload to S3"), function () {
                frappe.confirm(
                    __("This will upload the file to S3. Continue?"),
                    function () {
                        frappe.call({
                            method: "aws_integration.api.s3.upload_single_file_to_s3",
                            args: { file_name: frm.doc.name },
                            freeze: true,
                            freeze_message: __("Queuing file for S3 upload..."),
                            callback: function (r) {
                                if (r.message && r.message.success) {
                                    frappe.show_alert({
                                        message: __("File queued for S3 upload. The form will refresh automatically when complete."),
                                        indicator: "blue",
                                    }, 7);
                                } else {
                                    frappe.msgprint({
                                        title: __("Error"),
                                        indicator: "red",
                                        message: r.message
                                            ? r.message.message
                                            : __("Failed to queue file for S3 upload."),
                                    });
                                }
                            },
                            error: function () {
                                frappe.msgprint({
                                    title: __("Error"),
                                    indicator: "red",
                                    message: __("Failed to queue file for S3 upload."),
                                });
                            },
                        });
                    }
                );
            });
        }

        // Refresh the form when this specific file finishes uploading to S3
        // in the background. Use .off() to prevent duplicate listeners on refresh.
        frappe.realtime.off("s3_upload_complete");
        frappe.realtime.on("s3_upload_complete", function (data) {
            if (data.file_name === frm.doc.name) {
                frm.reload_doc();
            }
        });
    },
});

// With S3 switched off the File form shows nothing about S3: no fields, no status, no
// buttons. The one exception is a file that is already on S3, for System Managers,
// while the bucket can be reached: that is where they still need the S3 details and
// tools. The server sends both facts (see add_s3_form_context).
function s3_ui_visible(frm) {
    const s3 = (frm.doc.__onload || {}).s3 || {};
    if (s3.enabled !== false) {
        return true;
    }
    return frappe.user.has_role("System Manager") && s3.state === "stored" && s3.connected === true;
}

const S3_FIELDS = [
    "s3_info_section",
    "s3_key",
    "s3_status_section",
    "is_on_s3",
    "s3_uploaded_at",
    "s3_col_break",
    "local_deleted",
    "s3_upload_skipped",
];

function set_s3_fields_hidden(frm, hidden) {
    S3_FIELDS.forEach(function (name) {
        if (frm.fields_dict[name]) {
            frm.set_df_property(name, "hidden", hidden ? 1 : 0);
        }
    });
}
