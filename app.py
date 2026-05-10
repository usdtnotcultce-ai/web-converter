import os
import sys
import re
import uuid
import subprocess
import threading
from flask import Flask, request, render_template, send_file, jsonify
from PIL import Image
from pillow_heif import register_heif_opener
from pypdf import PdfReader
from pdf2docx import Converter
from docx import Document

register_heif_opener()

app = Flask(__name__)
UPLOAD_FOLDER = "uploads"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024

tasks = {}

# 🔍 Ищем ffmpeg и ffprobe рядом с app.py или в системном PATH
def get_bin_path(name):
    local = os.path.join(os.path.dirname(os.path.abspath(__file__)), f"{name}.exe")
    return local if os.path.exists(local) else name

FFMPEG = get_bin_path("ffmpeg")
FFPROBE = get_bin_path("ffprobe")

# Проверка при старте
for tool, name in [(FFMPEG, "FFmpeg"), (FFPROBE, "FFprobe")]:
    try:
        subprocess.run([tool, "-version"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True)
        print(f"✅ {name} найден")
    except Exception as e:
        print(f"❌ {name} не найден! Скачай static build, положи {name}.exe в папку с app.py или добавь в PATH.")

def get_media_duration(path):
    try:
        res = subprocess.run([FFPROBE, '-v', 'error', '-show_entries', 'format=duration', '-of', 'csv=p=0', path], capture_output=True, text=True, timeout=5)
        return float(res.stdout.strip())
    except: return None

def run_ffmpeg_with_progress(cmd, task_id, in_path):
    cmd[0] = FFMPEG  # Гарантируем абсолютный путь
    total = get_media_duration(in_path)
    
    # CREATE_NO_WINDOW скрывает чёрное окно FFmpeg на Windows
    startup_flags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
    proc = subprocess.Popen(cmd, stderr=subprocess.PIPE, stdout=subprocess.PIPE, text=True, encoding='utf-8', errors='replace', creationflags=startup_flags)
    
    time_pat = re.compile(r'time=(\d+:\d+:\d+\.\d+)')
    while True:
        line = proc.stderr.readline()
        if not line and proc.poll() is not None: break
        m = time_pat.search(line)
        if m and total:
            h, m, s = m.group(1).split(':')
            cur = float(h)*3600 + float(m)*60 + float(s)
            pct = min(int((cur / total) * 100), 99)
            tasks[task_id]['progress'] = pct
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"Ошибка FFmpeg (код {proc.returncode})")

def run_conversion(task_id, in_path, out_ext, quality):
    out_path = os.path.join(UPLOAD_FOLDER, f"{task_id}.{out_ext}")
    tasks[task_id]['status'] = 'processing'
    tasks[task_id].pop('progress', None)
    try:
        in_ext = in_path.rsplit('.', 1)[-1].lower()
        
        if in_ext in ('png', 'jpg', 'jpeg', 'webp', 'bmp', 'gif', 'tiff', 'heic', 'heif'):
            out_fmt = out_ext.upper()
            if out_fmt == 'JPG': out_fmt = 'JPEG'
            with Image.open(in_path) as img:
                if out_fmt == 'JPEG' and img.mode in ('RGBA', 'LA', 'P'): img = img.convert('RGB')
                q_map = {'low': 50, 'medium': 75, 'high': 90}
                save_kw = {'format': out_fmt}
                if out_fmt in ('JPEG', 'WEBP'): save_kw['quality'] = q_map.get(quality, 75)
                img.save(out_path, **save_kw)

        elif in_ext in ('pdf', 'docx'):
            if in_ext == 'pdf' and out_ext == 'docx':
                cv = Converter(in_path); cv.convert(out_path, start=0, end=None); cv.close()
            elif out_ext == 'txt':
                text = "\n".join(p.extract_text() or "" for p in PdfReader(in_path).pages) if in_ext=='pdf' else "\n".join(p.text for p in Document(in_path).paragraphs)
                with open(out_path, 'w', encoding='utf-8') as f: f.write(text)
            else: raise ValueError(f"{in_ext} -> {out_ext} не поддерживается")

        elif in_ext in ('mp4', 'webm', 'mkv', 'avi', 'mov', 'mp3', 'wav', 'aac', 'flac', 'ogg'):
            cmd = ['ffmpeg', '-i', in_path, '-y']
            if out_ext in ('mp4', 'webm', 'mkv', 'avi', 'mov'):
                if out_ext == 'webm':
                    cmd += ['-c:v', 'libvpx-vp9', '-crf', '32', '-b:v', '0', '-pix_fmt', 'yuv420p', '-c:a', 'libopus', '-b:a', '192k']
                else:
                    crf = {'low':'28','medium':'23','high':'18'}.get(quality, '23')
                    cmd += ['-c:v', 'libx264', '-crf', crf, '-preset', 'medium', '-pix_fmt', 'yuv420p', '-c:a', 'aac', '-b:a', '192k']
            else:
                cmd += ['-vn']
                br = {'low':'128k','medium':'192k','high':'320k'}.get(quality, '192k')
                codec = 'libmp3lame' if out_ext=='mp3' else ('copy' if out_ext in ('wav','flac') else 'aac')
                cmd += ['-c:a', codec, '-b:a', br] if codec!='copy' else ['-c:a','copy']
            cmd.append(out_path)
            run_ffmpeg_with_progress(cmd, task_id, in_path)
        else:
            raise ValueError("Тип файла не поддерживается")

        tasks[task_id]['status'] = 'done'
        tasks[task_id]['progress'] = 100
        tasks[task_id]['out_path'] = out_path
    except subprocess.TimeoutExpired: tasks[task_id]['status'] = 'timeout'
    except Exception as e: tasks[task_id]['status'] = f'error: {str(e)[:150]}'
    finally:
        if os.path.exists(in_path): os.remove(in_path)

@app.route('/')
def index(): return render_template('index.html')

@app.route('/upload', methods=['POST'])
def upload():
    if 'file' not in request.files: return jsonify({'error': 'Нет файла'}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({'error': 'Файл не выбран'}), 400
    ext = request.form.get('format', 'mp4')
    quality = request.form.get('quality', 'medium')
    task_id = str(uuid.uuid4())
    in_path = os.path.join(UPLOAD_FOLDER, f"{task_id}_{file.filename}")
    file.save(in_path)
    tasks[task_id] = {'status': 'queued', 'progress': 0}
    threading.Thread(target=run_conversion, args=(task_id, in_path, ext, quality), daemon=True).start()
    return jsonify({'task_id': task_id})

@app.route('/status/<task_id>')
def status(task_id):
    return jsonify(tasks.get(task_id, {'status': 'not_found'}))

@app.route('/download/<task_id>')
def download(task_id):
    task = tasks.get(task_id)
    if not task or task.get('status') != 'done': return jsonify({'error': 'Файл ещё не готов'}), 400
    return send_file(task['out_path'], as_attachment=True)

if __name__ == '__main__':
    print("🌐 Сервер: http://localhost:5000")
    app.run(host='0.0.0.0', port=5000, debug=False)