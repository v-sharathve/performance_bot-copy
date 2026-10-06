import re

APPLICATION_INSIGHTS_CONNECTION_STRING = "APPLICATIONINSIGHTS_CONNECTION_STRING"
APP_NAME = "gpt-rag-ui"

# Constants
UUID_REGEX = re.compile(
    r'^\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\s+',
    re.IGNORECASE
)

SUPPORTED_EXTENSIONS = [
    "pdf", "bmp", "jpeg", "jpg", "png", "tiff", "xlsx", "docx", "pptx",
    "md", "txt", "html", "shtml", "htm", "py", "csv", "xml", "json", "vtt"
]

REFERENCE_REGEX = re.compile(
    r'\[([^\]]+)\]\(([^)]+\.(?:' + '|'.join(SUPPORTED_EXTENSIONS) + r'))\)',
    re.IGNORECASE
)

TERMINATE_TOKEN = "TERMINATE"