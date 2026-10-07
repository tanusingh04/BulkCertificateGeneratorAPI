"""Certificate template constants and styling coordinates for Landscape A4."""

from reportlab.lib.colors import HexColor
from reportlab.lib.pagesizes import A4, landscape

# Page Dimensions (Landscape A4 in points: 841.89 x 595.27)
PAGE_WIDTH, PAGE_HEIGHT = landscape(A4)

# Color Palette
COLOR_PRIMARY = HexColor("#1E3A8A")     # Deep Classic Blue
COLOR_SECONDARY = HexColor("#B45309")   # Muted Gold / Amber
COLOR_TEXT_MAIN = HexColor("#111827")   # Dark Slate / Almost Black
COLOR_TEXT_MUTED = HexColor("#4B5563")  # Slate Gray
COLOR_BORDER_BG = HexColor("#F8FAFC")   # Ultra light border backdrop

# Margins and Borders
BORDER_OUTER_MARGIN = 24
BORDER_INNER_MARGIN = 30
BORDER_OUTER_WIDTH = 3
BORDER_INNER_WIDTH = 1

# QR Code geometry
QR_SIZE = 75
QR_X = PAGE_WIDTH - BORDER_OUTER_MARGIN - QR_SIZE - 30
QR_Y = BORDER_OUTER_MARGIN + 35
