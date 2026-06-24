#!/usr/bin/env python3
"""Replace photo in PDF"""

from reportlab.pdfgen import canvas
from reportlab.lib.pagesizes import letter
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Image as RLImage, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.lib import colors
from pypdf import PdfReader, PdfWriter
from PIL import Image
import os

# Paths
pdf_path = r'C:\Users\idanb\Desktop\Claude\CEACAA00FKBZDN.PDF'
new_image_path = r'C:\Users\idanb\Desktop\Claude\WhatsApp Image 2026-06-22 at 13.57.44.jpeg'
output_path = r'C:\Users\idanb\Desktop\Claude\CEACAA00FKBZDN_updated.pdf'

# Read original PDF
print("Reading original PDF...")
reader = PdfReader(pdf_path)
print(f"Original PDF has {len(reader.pages)} pages")

# Create new PDF with updated photo
print("Creating new PDF with updated photo...")
doc = SimpleDocTemplate(output_path, pagesize=letter, topMargin=0.5*inch, bottomMargin=0.5*inch)
story = []
styles = getSampleStyleSheet()

# Read and resize the new image
img = Image.open(new_image_path)
# Resize to fit in the document (similar to original)
img_width = 2.0 * inch
img_height = int((img.height / img.width) * img_width)
new_image = RLImage(new_image_path, width=img_width, height=img_height)

# Add the new image
story.append(new_image)
story.append(Spacer(1, 0.3*inch))

# Extract text from the original PDF and add it
print("Extracting text from original PDF...")
for page_num, page in enumerate(reader.pages):
    text = page.extract_text()
    if text:
        # Add extracted text
        para = Paragraph(text.replace('\n', '<br/>'), styles['Normal'])
        story.append(para)
        if page_num < len(reader.pages) - 1:
            story.append(Spacer(1, 0.2*inch))

# Build the PDF
print("Building new PDF...")
doc.build(story)

print(f"✓ Done! Updated PDF saved to: {output_path}")
