# OCR language data

`fra.traineddata` and `eng.traineddata` come from the
[tesseract-ocr/tessdata_fast](https://github.com/tesseract-ocr/tessdata_fast) repository
(Apache License 2.0). They are used by the Tesseract engine built into MuPDF for the
"Make searchable (OCR)" tool.

To add another language, drop its `<lang>.traineddata` file from tessdata_fast into this folder.
