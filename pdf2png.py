import os
import glob
from pathlib import Path

try:
    import fitz  # PyMuPDF
except ImportError:
    print("Ошибка: библиотека PyMuPDF не установлена.")
    print("Выполните в терминале команду: python3 -m pip install pymupdf")
    exit()

def convert_all_pdfs_to_4k():
    # Находим текущую папку, где запущен скрипт
    current_dir = Path.cwd()
    
    # Ищем абсолютно все PDF файлы в этой папке
    pdf_files = glob.glob(str(current_dir / "*.pdf"))
    
    if not pdf_files:
        print("Ошибка: В папке со скриптом не найдено ни одного PDF файла!")
        return

    print(f"=== Найдено файлов для обработки: {len(pdf_files)} ===\n")
    
    # Цикл по всем найденным PDF файлам
    for pdf_file_path in pdf_files:
        pdf_path = Path(pdf_file_path)
        print(f"🎬 Обработка файла: {pdf_path.name}")
        
        # Создаем индивидуальную папку для картинок этого PDF
        output_folder = current_dir / f"{pdf_path.stem}_4K_Images"
        output_folder.mkdir(exist_ok=True)
        
        # Открываем PDF документ
        doc = fitz.open(pdf_path)
        
        # Проходим по каждой странице документа
        for page_num in range(len(doc)):
            page = doc.load_page(page_num)
            
            # Считаем коэффициент масштабирования под жесткое разрешение 4K (3840x2160)
            rect = page.rect
            zoom_x = 3840 / rect.width
            zoom_y = 2160 / rect.height
            
            # Создаем матрицу трансформации для рендера вектора без потери качества
            matrix = fitz.Matrix(zoom_x, zoom_y)
            pix = page.get_pixmap(matrix=matrix)
            
            # Формируем имя файла (например, Slide_001.png)
            output_filename = output_folder / f"Slide_{page_num + 1:03d}.png"
            
            # Сохраняем картинку
            pix.save(str(output_filename))
            
        print(f"✅ Готово! {len(doc)} слайдов сохранены в папку: {output_folder.name}\n")
        doc.close()
        
    print("=== Все PDF файлы успешно сконвертированы в 4K! ===")

if __name__ == "__main__":
    convert_all_pdfs_to_4k()

