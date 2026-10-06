"""File controller that understands S3-stored files.

Frappe uses only the LAST ``override_doctype_class["File"]`` entry, and other installed apps
override File too (for example drive_bucket). A class that extends core File directly would
silently drop every override registered before it, so the class exported as ``S3File`` is built
on the entry registered immediately before this app's own, or on core File when there is none.
Each app doing the same keeps all of their guards, whichever app is installed last.
"""

from frappe.core.doctype.file.file import File as CoreFile

import frappe

from aws_integration.s3 import S3_API_PREFIX

OWN_PATH = "aws_integration.s3.overrides.S3File"


class S3FileMixin:
    """Skip Frappe's disk-based validation for S3 API URLs.

    Frappe's dedup logic copies file_url from an existing File doc onto the new one when
    content_hash matches. When that file_url is our S3 API route, Frappe's validation methods
    reject it because it's not a local disk path.
    """

    def _is_s3_url(self):
        return bool(self.file_url and self.file_url.startswith(S3_API_PREFIX))

    def exists_on_disk(self):
        if self.is_on_s3 and self.s3_key:
            return True
        if self._is_s3_url():
            return True
        return super().exists_on_disk()

    def validate_file_path(self):
        if self._is_s3_url():
            return
        super().validate_file_path()

    def validate_file_url(self):
        if self._is_s3_url():
            return
        super().validate_file_url()

    def validate_file_on_disk(self):
        if self._is_s3_url():
            return
        super().validate_file_on_disk()


_classes: dict[type, type] = {}


def _base_class() -> type:
    """The File class registered right before this app's own entry, else core File."""
    paths = frappe.get_hooks("override_doctype_class").get("File", [])
    if OWN_PATH not in paths:
        return CoreFile
    index = paths.index(OWN_PATH)
    return frappe.get_attr(paths[index - 1]) if index else CoreFile


def __getattr__(name: str):
    # Resolved per lookup so each site layers on its own installed apps. Cached per base,
    # so a pickled document always finds the same class object again.
    if name != "S3File":
        raise AttributeError(name)
    base = _base_class()
    if base not in _classes:
        _classes[base] = type("S3File", (S3FileMixin, base), {"__module__": __name__})
    return _classes[base]
