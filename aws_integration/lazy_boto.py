import frappe
from frappe import _


def get_boto3():
	"""Import boto3 on first use instead of at module import.

	boto3 pulls in urllib3's pyOpenSSL support. When pyOpenSSL and cryptography are out of step it
	fails while being imported (AttributeError / ImportError), and a module-level `import boto3`
	then takes down everything that imports the module, including `bench migrate`. Importing it
	here keeps the failure to the code path that really needs AWS, with a message that says why.
	"""
	try:
		import boto3
	except Exception as e:
		frappe.log_error(title="AWS Integration: boto3 could not be loaded")
		frappe.throw(
			_(
				"The AWS library (boto3) could not be loaded: {0}. Check that the installed pyOpenSSL and cryptography versions match what Frappe requires."
			).format(f"{type(e).__name__}: {e}"),
			title=_("AWS Library Unavailable"),
		)
	return boto3
