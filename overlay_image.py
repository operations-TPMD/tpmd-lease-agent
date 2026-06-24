#!/usr/bin/env python3
"""Overlay new image on existing PDF page"""

from pypdf import PdfReader, PdfWriter
from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from io import BytesIO
from PIL import Image
import os

# Paths
pdf_path = r'C:\Users\idanb\Desktop\Claude\CEACAA00FKBZDN.PDF'
new_image_path = r'C:\Users\idanb\Desktop\Claude\WhatsApp Image 2026-06-22 at 13.57.44.jpeg'
output_path = r'C:\Users\idanb\Desktop\Claude\CEACAA00FKBZDN_updated.pdf'

# Read original PDF
print("Reading original PDF...")
reader = PdfReader(pdf_path)
writer = PdfWriter()

# Get the first page
first_page = reader.pages[0]
page_width = float(first_page.mediabox.width)
page_height = float(first_page.mediabox.height)

print(f"Page size: {page_width} x {page_height}")

# Create a canvas with the new image in the same position as the old one
# The old image was approximately at position (155, 310) with size 170x210
packet = BytesIO()
can = canvas.Canvas(packet, pagesize=(page_width, page_height))

# Load the image to get dimensions
img = Image.open(new_image_path)
aspect_ratio = img.height / img.width

# Image position and size (matching original position)
img_width = 170
img_height = int(img_width * aspect_ratio)
img_x = 155
img_y = page_height - 310 - img_height  # Convert from bottom-left to top-left coordinates

print(f"Adding image at position ({img_x}, {img_y}) with size {img_width}x{img_height}")

# Draw the image on the canvas
can.drawImage(new_image_path, img_x, img_y, width=img_width, height=img_height)
can.save()

# Move to the beginning of the StringIO buffer
packet.seek(0)
image_pdf = PdfReader(packet)
image_page = image_pdf.pages[0]

# Merge the image page with the original page
first_page.merge_page(image_page)

# Add the merged page to the writer
writer.add_page(first_page)

# Add remaining pages
for i in range(1, len(reader.pages)):
    writer.add_page(reader.pages[i])

# Write to output file
print(f"Saving to {output_path}...")
with open(output_path, 'wb') as f:
    writer.write(f)

print("✓ Done! PDF updated successfully.")
