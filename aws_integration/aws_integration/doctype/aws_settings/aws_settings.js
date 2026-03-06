// Copyright (c) 2024, Hybrowlabs Technologies and contributors
// For license information, please see license.txt

function show_region_picker(frm, provider) {
    frappe.call({
        method: "aws_integration.api.s3.get_s3_provider_regions",
        args: { provider: provider },
        callback: function (r) {
            let regions = r.message || [];
            if (!regions.length) {
                // MinIO, Custom — no predefined regions
                frm.set_value("s3_endpoint_url", "");
                return;
            }
            let options = regions.map((r) => r.label);
            let region_map = {};
            regions.forEach((r) => {
                region_map[r.label] = r;
            });

            let d = new frappe.ui.Dialog({
                title: __("Select {0} Region", [provider]),
                fields: [
                    {
                        fieldname: "provider_region",
                        fieldtype: "Select",
                        label: __("Region"),
                        options: options.join("\n"),
                        reqd: 1,
                    },
                ],
                primary_action_label: __("Set"),
                primary_action: function (values) {
                    let selected = region_map[values.provider_region];
                    if (selected) {
                        frm.set_value("s3_region", selected.region);
                        frm.set_value("s3_endpoint_url", selected.endpoint || "");
                    }
                    d.hide();
                },
            });
            d.show();
        },
    });
}

frappe.ui.form.on("AWS Settings", {
    s3_provider: function (frm) {
        let provider = frm.doc.s3_provider;
        if (provider === "AWS S3" || provider === "Other") {
            // AWS S3: boto3 handles regions/endpoints natively
            // Other: user fills endpoint and region manually
            frm.set_value("s3_endpoint_url", "");
            return;
        }
        if (provider === "Cloudflare R2") {
            frappe.prompt(
                {
                    fieldname: "account_id",
                    fieldtype: "Data",
                    label: __("Cloudflare Account ID"),
                    reqd: 1,
                },
                function (values) {
                    let account_id = values.account_id.trim().replace(/[^a-zA-Z0-9-]/g, "");
                    if (!account_id) {
                        frappe.msgprint(__("Invalid Account ID"));
                        return;
                    }
                    frm.set_value(
                        "s3_endpoint_url",
                        `https://${account_id}.r2.cloudflarestorage.com`
                    );
                    frm.set_value("s3_region", "auto");
                },
                __("Cloudflare R2 Configuration")
            );
            return;
        }
        show_region_picker(frm, provider);
    },
    refresh: function (frm) {
        if (frm.doc.enable_aws && frm.doc.enable_s3) {
            frm.add_custom_button(
                __("Test S3 Connection"),
                function () {
                    frappe.call({
                        method: "aws_integration.api.s3.test_s3_connection",
                        freeze: true,
                        freeze_message: __("Testing S3 Connection..."),
                        callback: function (r) {
                            if (r.message && r.message.success) {
                                frappe.msgprint({
                                    title: __("Success"),
                                    indicator: "green",
                                    message: r.message.message,
                                });
                            } else {
                                frappe.msgprint({
                                    title: __("Failed"),
                                    indicator: "red",
                                    message: r.message
                                        ? r.message.message
                                        : __("Connection failed"),
                                });
                            }
                        },
                    });
                },
                __("S3 Files")
            );

            frm.add_custom_button(
                __("Migrate All Files to S3"),
                function () {
                    frappe.confirm(
                        __(
                            "This will upload all local files to S3. This may take a while. Continue?"
                        ),
                        function () {
                            frappe.call({
                                method: "aws_integration.api.s3.migrate_files_to_s3",
                                freeze: true,
                                freeze_message: __(
                                    "Starting file migration..."
                                ),
                                callback: function (r) {
                                    if (r.message) {
                                        frappe.msgprint(r.message);
                                    }
                                },
                                error: function () {
                                    frappe.msgprint({
                                        title: __("Error"),
                                        indicator: "red",
                                        message: __("Failed to start file migration."),
                                    });
                                },
                            });
                        }
                    );
                },
                __("S3 Files")
            );

            frm.add_custom_button(
                __("S3 Status"),
                function () {
                    frappe.call({
                        method: "aws_integration.api.s3.get_s3_status",
                        freeze: true,
                        freeze_message: __("Fetching S3 status..."),
                        callback: function (r) {
                            if (!r.message) return;
                            let d = r.message;
                            let fmt_size = function (bytes) {
                                if (!bytes) return "0 B";
                                let units = ["B", "KB", "MB", "GB", "TB"];
                                let i = Math.floor(Math.log(bytes) / Math.log(1024));
                                return (bytes / Math.pow(1024, i)).toFixed(1) + " " + units[i];
                            };
                            let esc = frappe.utils.xss_sanitise;
                            let s_on_s3 = cint(d.on_s3);
                            let s_pending = cint(d.pending);
                            let s_exempt = cint(d.exempt);
                            let s_total = cint(d.total_files);
                            let s_errors = cint(d.recent_errors);
                            let s_skipped = cint(d.recent_skipped);
                            let pct = s_total
                                ? ((s_on_s3 / s_total) * 100).toFixed(1)
                                : 0;

                            let last_upload_str = "";
                            if (d.last_uploaded_at) {
                                last_upload_str = __("Last upload: {0}", [esc(frappe.datetime.prettyDate(d.last_uploaded_at))]);
                            } else {
                                last_upload_str = __("No files uploaded yet");
                            }

                            let html = `
                                <div class="s3-status-grid" style="display:grid; grid-template-columns:1fr 1fr; gap:12px;">
                                    <div class="s3-stat" style="padding:12px; border-radius:8px; background:var(--bg-light-gray);">
                                        <div style="font-size:11px; color:var(--text-muted); text-transform:uppercase;">${__("On S3")}</div>
                                        <div style="font-size:22px; font-weight:600; color:var(--text-color);">${s_on_s3}</div>
                                        <div style="font-size:12px; color:var(--text-muted);">${fmt_size(d.s3_size)}</div>
                                    </div>
                                    <div class="s3-stat" style="padding:12px; border-radius:8px; background:var(--bg-light-gray);">
                                        <div style="font-size:11px; color:var(--text-muted); text-transform:uppercase;">${__("Pending Upload")}</div>
                                        <div style="font-size:22px; font-weight:600; color:${s_pending ? 'var(--orange-500)' : 'var(--text-color)'};">${s_pending}</div>
                                        <div style="font-size:12px; color:var(--text-muted);">${fmt_size(d.pending_size)}</div>
                                    </div>
                                    <div class="s3-stat" style="padding:12px; border-radius:8px; background:var(--bg-light-gray);">
                                        <div style="font-size:11px; color:var(--text-muted); text-transform:uppercase;">${__("Exempt Files")}</div>
                                        <div style="font-size:22px; font-weight:600; color:var(--text-color);">${s_exempt}</div>
                                    </div>
                                    <div class="s3-stat" style="padding:12px; border-radius:8px; background:var(--bg-light-gray);">
                                        <div style="font-size:11px; color:var(--text-muted); text-transform:uppercase;">${__("Total Files")}</div>
                                        <div style="font-size:22px; font-weight:600; color:var(--text-color);">${s_total}</div>
                                    </div>
                                </div>
                                <div style="margin-top:16px;">
                                    <div style="display:flex; justify-content:space-between; margin-bottom:4px;">
                                        <span style="font-size:12px; color:var(--text-muted);">${__("S3 Migration Progress")}</span>
                                        <span style="font-size:12px; font-weight:600;">${pct}%</span>
                                    </div>
                                    <div style="height:8px; background:var(--bg-light-gray); border-radius:4px; overflow:hidden;">
                                        <div style="height:100%; width:${pct}%; background:var(--primary); border-radius:4px; transition:width 0.3s;"></div>
                                    </div>
                                </div>
                                <div style="margin-top:12px; font-size:12px; color:var(--text-muted);">
                                    ${last_upload_str}
                                    ${s_errors
                                        ? ' &middot; <span style="color:var(--red-500);">' + __("{0} errors in last 7 days", [s_errors]) + '</span>'
                                        : ''}
                                    ${s_skipped
                                        ? ' &middot; <span style="color:var(--orange-500);">' + __("{0} files skipped (not found on disk)", [s_skipped]) + '</span>'
                                        : ''}
                                </div>
                            `;

                            frappe.msgprint({
                                title: __("S3 Storage Status"),
                                message: html,
                                wide: true,
                                indicator: d.pending ? "orange" : "green",
                            });
                        },
                    });
                },
                __("S3 Files")
            );

            frm.add_custom_button(
                __("Clean Up Local Files"),
                function () {
                    frappe.confirm(
                        __(
                            "This will delete local copies of files already uploaded to S3. This cannot be undone. Continue?"
                        ),
                        function () {
                            frappe.call({
                                method: "aws_integration.api.s3.cleanup_local_s3_files",
                                freeze: true,
                                freeze_message: __("Starting local cleanup..."),
                                callback: function (r) {
                                    if (r.message) {
                                        frappe.msgprint(r.message);
                                    }
                                },
                                error: function () {
                                    frappe.msgprint({
                                        title: __("Error"),
                                        indicator: "red",
                                        message: __("Failed to start local cleanup."),
                                    });
                                },
                            });
                        }
                    );
                },
                __("S3 Files")
            );

            frm.add_custom_button(
                __("Adopt Orphaned Files"),
                function () {
                    frappe.confirm(
                        __(
                            "This will scan public/files and private/files for files not tracked by any File document, "
                            + "create File records for them, and queue them for S3 upload. Continue?"
                        ),
                        function () {
                            frappe.call({
                                method: "aws_integration.api.s3.adopt_orphaned_files",
                                freeze: true,
                                freeze_message: __("Scanning for orphaned files..."),
                                callback: function (r) {
                                    if (r.message) {
                                        frappe.msgprint(r.message);
                                    }
                                },
                                error: function () {
                                    frappe.msgprint({
                                        title: __("Error"),
                                        indicator: "red",
                                        message: __("Failed to scan for orphaned files."),
                                    });
                                },
                            });
                        }
                    );
                },
                __("S3 Files")
            );

        }

        if (frm.doc.enable_s3_backups) {
            frm.add_custom_button(
                __("Take Backup Now"),
                function () {
                    frappe.confirm(
                        __("This will generate a full site backup and upload it to S3. Continue?"),
                        function () {
                            frappe.call({
                                method: "aws_integration.s3.backup.take_s3_backup",
                                freeze: true,
                                freeze_message: __("Queuing S3 Backup..."),
                                callback: function (r) {
                                    if (r.message && r.message.log_name) {
                                        frm._s3_queued_logs = [r.message.log_name];
                                        frm._s3_total_count = 1;
                                        frm._s3_completed_count = 0;
                                        frm._s3_upload_only = false;
                                        frm._s3_active_stage = "queued";
                                        render_backup_status_card(frm, {
                                            name: r.message.log_name,
                                            status: "Queued",
                                        });
                                    }
                                },
                                error: function () {
                                    frappe.msgprint({
                                        title: __("Error"),
                                        indicator: "red",
                                        message: __("Failed to start S3 backup."),
                                    });
                                },
                            });
                        }
                    );
                },
                __("S3 Backups")
            );

            frm.add_custom_button(
                __("Upload Local Backups"),
                function () {
                    frappe.confirm(
                        __("This will scan for existing local backups and upload them to S3. Continue?"),
                        function () {
                            frappe.call({
                                method: "aws_integration.s3.backup.upload_local_backups",
                                freeze: true,
                                freeze_message: __("Scanning local backups..."),
                                callback: function (r) {
                                    if (r.message) {
                                        frm._s3_queued_logs = r.message.queued || [];
                                        frm._s3_total_count = r.message.count || 0;
                                        frm._s3_completed_count = 0;
                                        frm._s3_upload_only = true;
                                        frm._s3_active_stage = "queued";
                                        frappe.msgprint({
                                            title: __("Local Backups Queued"),
                                            indicator: "blue",
                                            message: __("{0} backup(s) queued for upload.", [r.message.count]),
                                        });
                                        fetch_and_render_backup_card(frm);
                                    }
                                },
                            });
                        }
                    );
                },
                __("S3 Backups")
            );

            frm.add_custom_button(
                __("Backup Logs"),
                function () {
                    frappe.set_route("List", "S3 Backup Log");
                },
                __("S3 Backups")
            );

            // Render initial status card from latest backup log
            fetch_and_render_backup_card(frm);
        }

        if (frm.doc.enable_aws && frm.doc.enable_s3) {
            frappe.realtime.off("s3_migration_progress");
            frappe.realtime.on("s3_migration_progress", function (data) {
                frappe.show_progress(
                    __("S3 Migration"),
                    data.uploaded + data.failed,
                    data.total,
                    __("{0} uploaded, {1} failed", [data.uploaded, data.failed])
                );
            });

            frappe.realtime.off("s3_migration_complete");
            frappe.realtime.on("s3_migration_complete", function (data) {
                frappe.hide_progress();
                frappe.msgprint({
                    title: __("S3 Migration Complete"),
                    indicator: data.failed ? "orange" : "green",
                    message: __("{0} files uploaded, {1} failed", [data.uploaded, data.failed]),
                });
            });

            frappe.realtime.off("s3_cleanup_progress");
            frappe.realtime.on("s3_cleanup_progress", function (data) {
                frappe.show_progress(
                    __("S3 Local Cleanup"),
                    data.deleted + data.missing + data.skipped,
                    data.total,
                    __("{0} deleted, {1} already gone", [data.deleted, data.missing])
                );
            });

            frappe.realtime.off("s3_cleanup_complete");
            frappe.realtime.on("s3_cleanup_complete", function (data) {
                frappe.hide_progress();
                frappe.msgprint({
                    title: __("S3 Local Cleanup Complete"),
                    indicator: data.skipped ? "orange" : "green",
                    message: __("{0} local files deleted, {1} already removed, {2} skipped", [
                        data.deleted, data.missing, data.skipped
                    ]),
                });
            });

            frappe.realtime.off("s3_orphan_progress");
            frappe.realtime.on("s3_orphan_progress", function (data) {
                frappe.show_alert(
                    __("{0} orphaned files adopted, {1} errors so far...", [
                        data.adopted, data.errors
                    ]),
                    5
                );
            });

            frappe.realtime.off("s3_orphan_complete");
            frappe.realtime.on("s3_orphan_complete", function (data) {
                frappe.msgprint({
                    title: __("Orphan Adoption Complete"),
                    indicator: data.errors ? "orange" : "green",
                    message: __("{0} orphaned files adopted, {1} errors", [
                        data.adopted, data.errors
                    ]),
                });
            });

            frappe.realtime.off("s3_backup_progress");
            frappe.realtime.on("s3_backup_progress", function (data) {
                // Ignore events not belonging to our tracked batch
                let queued = frm._s3_queued_logs || [];
                if (queued.length && data.log_name && queued.indexOf(data.log_name) === -1) {
                    return;
                }

                if (data.status === "generating") {
                    frm._s3_active_stage = "generating";
                    update_stage_pipeline(frm, "generating");
                } else if (data.status === "uploading") {
                    frm._s3_active_stage = "uploading";
                    update_stage_pipeline(frm, "uploading");
                } else if (data.status === "success" || data.status === "failed") {
                    // Remove completed log from the queue
                    if (data.log_name && queued.length) {
                        let idx = queued.indexOf(data.log_name);
                        if (idx !== -1) {
                            queued.splice(idx, 1);
                        }
                        frm._s3_completed_count = (frm._s3_completed_count || 0) + 1;
                    }

                    if (queued.length) {
                        // More backups remain — show the next one
                        frm._s3_active_stage = "queued";
                        fetch_and_render_backup_card(frm);
                    } else {
                        // All done — clear tracking state
                        frm._s3_queued_logs = [];
                        frm._s3_total_count = 0;
                        frm._s3_completed_count = 0;
                        frm._s3_upload_only = false;
                        frm._s3_active_stage = null;
                        fetch_and_render_backup_card(frm);
                    }
                }
            });
        }
    },
});

// ── Backup Status Card helpers ──

const STAGES = ["queued", "generating", "uploading", "done"];
const STAGES_UPLOAD = ["queued", "uploading", "done"];

const STAGE_LABELS = {
    queued: __("Queued"),
    generating: __("Generating"),
    uploading: __("Uploading"),
    done: __("Done"),
};

function fmt_size(bytes) {
    if (!bytes) return "0 B";
    let units = ["B", "KB", "MB", "GB", "TB"];
    let i = Math.floor(Math.log(bytes) / Math.log(1024));
    return (bytes / Math.pow(1024, i)).toFixed(1) + " " + units[i];
}

function map_log_status_to_stage(status) {
    switch (status) {
        case "Queued": return "queued";
        case "Generating": return "generating";
        case "Uploading": return "uploading";
        case "Success": return "done";
        case "Failed": return "failed";
        default: return null;
    }
}

function fetch_and_render_backup_card(frm) {
    let queued = frm._s3_queued_logs || [];
    let fetch_args;

    if (queued.length) {
        // Fetch the first (currently active) log from the tracked batch
        fetch_args = {
            doctype: "S3 Backup Log",
            fields: [
                "name", "status", "started_at", "completed_at",
                "total_size", "db_size", "files_size", "s3_bucket", "creation",
            ],
            filters: { name: queued[0] },
            limit_page_length: 1,
        };
    } else {
        // No active batch — fetch the latest log by creation
        fetch_args = {
            doctype: "S3 Backup Log",
            fields: [
                "name", "status", "started_at", "completed_at",
                "total_size", "db_size", "files_size", "s3_bucket", "creation",
            ],
            order_by: "creation desc",
            limit_page_length: 1,
        };
    }

    frappe.call({
        method: "frappe.client.get_list",
        args: fetch_args,
        async: true,
        callback: function (r) {
            if (r.message && r.message.length) {
                let log = r.message[0];
                let stage = map_log_status_to_stage(log.status);
                // If a backup is in progress, the realtime events will
                // have set _s3_active_stage more precisely
                if ((log.status === "Generating" || log.status === "Uploading") && frm._s3_active_stage) {
                    stage = frm._s3_active_stage;
                }

                let batch_info = null;
                let total = frm._s3_total_count || 0;
                if (total > 1) {
                    batch_info = {
                        current: (frm._s3_completed_count || 0) + 1,
                        total: total,
                    };
                }

                render_backup_status_card(frm, log, stage, batch_info);
            } else {
                render_empty_backup_card(frm);
            }
        },
    });
}

function render_empty_backup_card(frm) {
    let wrapper = frm.fields_dict.s3_backup_status_html;
    if (!wrapper) return;
    wrapper.$wrapper.html(`
        <div class="s3-backup-card" style="
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 24px;
            margin-bottom: 16px;
            text-align: center;
            color: var(--text-muted);
        ">
            <div style="font-size: 14px;">${__("No backups yet")}</div>
            <div style="font-size: 12px; margin-top: 4px;">
                ${__('Click "Take Backup Now" to create your first S3 backup.')}
            </div>
        </div>
    `);
}

function render_backup_status_card(frm, log, stage, batch_info) {
    let wrapper = frm.fields_dict.s3_backup_status_html;
    if (!wrapper) return;

    let is_failed = log.status === "Failed";
    if (!stage) stage = map_log_status_to_stage(log.status);

    let stages = frm._s3_upload_only ? STAGES_UPLOAD : STAGES;
    let pipeline_html = build_stage_pipeline(stage, is_failed, stages);
    let summary_html = build_summary(log, batch_info);

    wrapper.$wrapper.html(`
        <div class="s3-backup-card" style="
            border: 1px solid var(--border-color);
            border-radius: 8px;
            overflow: hidden;
            margin-bottom: 16px;
        ">
            <div class="s3-backup-pipeline" style="padding: 16px 20px; background: var(--fg-color);">
                ${pipeline_html}
            </div>
            <div class="s3-backup-summary" style="
                padding: 12px 20px;
                border-top: 1px solid var(--border-color);
                font-size: 13px;
                color: var(--text-color);
                background: var(--fg-color);
            ">
                ${summary_html}
            </div>
        </div>
        <style>
            @keyframes s3-pulse {
                0%, 100% { opacity: 1; }
                50% { opacity: 0.4; }
            }
            .s3-stage-active .s3-stage-dot {
                animation: s3-pulse 1.5s ease-in-out infinite;
            }
        </style>
    `);
}

function build_stage_pipeline(active_stage, is_failed, stages) {
    if (!stages) stages = STAGES;
    let active_idx = stages.indexOf(active_stage);
    // For "failed", mark up to the last reached stage
    let failed_stage = null;
    if (is_failed) {
        // If active_stage is "failed" (not in stages), determine from context
        if (active_idx === -1) {
            // Unknown stage — mark the first stage as failed
            active_idx = 0;
        }
        failed_stage = active_idx;
    }

    let items = stages.map(function (stage, idx) {
        let dot_style, label_style, css_class;

        if (is_failed && idx === failed_stage) {
            // Failed at this stage
            dot_style = "background: var(--red-500); border-color: var(--red-500);";
            label_style = "color: var(--red-500); font-weight: 600;";
            css_class = "";
        } else if (is_failed && idx < failed_stage) {
            // Completed before failure
            dot_style = "background: var(--green-500); border-color: var(--green-500);";
            label_style = "color: var(--green-500);";
            css_class = "";
        } else if (!is_failed && active_idx >= 0 && idx < active_idx) {
            // Completed stage
            dot_style = "background: var(--green-500); border-color: var(--green-500);";
            label_style = "color: var(--green-500);";
            css_class = "";
        } else if (!is_failed && idx === active_idx) {
            // Active/in-progress stage
            if (stage === "done") {
                // Done = fully completed
                dot_style = "background: var(--green-500); border-color: var(--green-500);";
                label_style = "color: var(--green-500); font-weight: 600;";
                css_class = "";
            } else {
                dot_style = "background: var(--yellow-500); border-color: var(--yellow-500);";
                label_style = "color: var(--yellow-500); font-weight: 600;";
                css_class = "s3-stage-active";
            }
        } else {
            // Pending
            dot_style = "background: transparent; border-color: var(--gray-400);";
            label_style = "color: var(--gray-400);";
            css_class = "";
        }

        return `<div class="s3-stage-item ${css_class}" style="display: flex; align-items: center; gap: 6px;">
            <span class="s3-stage-dot" style="
                display: inline-block;
                width: 12px; height: 12px;
                border-radius: 50%;
                border: 2px solid;
                ${dot_style}
                flex-shrink: 0;
            "></span>
            <span style="font-size: 12px; white-space: nowrap; ${label_style}">${STAGE_LABELS[stage]}</span>
        </div>`;
    });

    let connector = `<div style="flex: 1; height: 2px; background: var(--border-color); margin: 0 4px;"></div>`;
    return `<div style="display: flex; align-items: center;">${items.join(connector)}</div>`;
}

function build_summary(log, batch_info) {
    let safe_name = frappe.utils.xss_sanitise(log.name || "");
    let link = `<a href="/app/s3-backup-log/${encodeURIComponent(safe_name)}">${safe_name}</a>`;

    let badge = "";
    switch (log.status) {
        case "Success":
            badge = `<span style="
                display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; font-weight: 600;
                background: var(--green-100); color: var(--green-700);
            ">${__("Success")}</span>`;
            break;
        case "Failed":
            badge = `<span style="
                display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; font-weight: 600;
                background: var(--red-100); color: var(--red-700);
            ">${__("Failed")}</span>`;
            break;
        case "Generating":
            badge = `<span style="
                display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; font-weight: 600;
                background: var(--yellow-100); color: var(--yellow-700);
            ">${__("Generating")}</span>`;
            break;
        case "Uploading":
            badge = `<span style="
                display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; font-weight: 600;
                background: var(--orange-100); color: var(--orange-700);
            ">${__("Uploading")}</span>`;
            break;
        case "Queued":
            badge = `<span style="
                display: inline-block; padding: 1px 8px; border-radius: 10px; font-size: 11px; font-weight: 600;
                background: var(--blue-100); color: var(--blue-700);
            ">${__("Queued")}</span>`;
            break;
    }

    let size_str = log.total_size ? fmt_size(log.total_size) : "";
    let time_str = "";
    let ref_time = log.completed_at || log.started_at || log.creation;
    if (ref_time) {
        time_str = frappe.datetime.prettyDate(ref_time);
    }

    let top_line = [link, badge, size_str, time_str]
        .filter(Boolean)
        .join(" &nbsp;&middot;&nbsp; ");

    let details = [];
    if (log.db_size) details.push(__("DB: {0}", [fmt_size(log.db_size)]));
    if (log.files_size) details.push(__("Files: {0}", [fmt_size(log.files_size)]));
    if (log.s3_bucket) details.push(__("Bucket: {0}", [frappe.utils.xss_sanitise(log.s3_bucket)]));

    let bottom_line = details.length
        ? `<div style="font-size: 12px; color: var(--text-muted); margin-top: 4px;">${details.join(" &nbsp;&middot;&nbsp; ")}</div>`
        : "";

    let batch_line = "";
    if (batch_info && batch_info.total > 1) {
        batch_line = `<div style="font-size: 12px; color: var(--blue-500); font-weight: 600; margin-top: 4px;">${__("Backup {0} of {1}", [batch_info.current, batch_info.total])}</div>`;
    }

    return `<div>${top_line}</div>${bottom_line}${batch_line}`;
}

function update_stage_pipeline(frm, stage) {
    let wrapper = frm.fields_dict.s3_backup_status_html;
    if (!wrapper) return;

    let pipeline_el = wrapper.$wrapper.find(".s3-backup-pipeline");
    if (!pipeline_el.length) {
        // Card hasn't been rendered yet — render a minimal one
        render_backup_status_card(frm, {
            name: (frm._s3_queued_logs || [])[0] || "",
            status: "In Progress",
        }, stage);
        return;
    }

    let stages = frm._s3_upload_only ? STAGES_UPLOAD : STAGES;
    pipeline_el.html(build_stage_pipeline(stage, false, stages));
}
