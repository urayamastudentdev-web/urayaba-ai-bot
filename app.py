import os
import io
import datetime
import time
import tempfile
from flask import Flask, render_template, request, jsonify
import google.generativeai as genai
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaIoBaseDownload
import gspread

app = Flask(__name__)

# --- Configuration ---
GENAI_API_KEY = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=GENAI_API_KEY)

SERVICE_ACCOUNT_FILE = '/etc/secrets/credentials.json'

# Drive Folder ID
DRIVE_FOLDER_ID = '1fJ3Mbrcw-joAsX33aBu0z4oSQu7I0PhP' 

# Spreadsheet ID
SPREADSHEET_ID = '1NK0ixXY9hOWuMib22wZxmFX6apUV7EhTDawTXPganZg'

# --- Model Setting (Multimodal) ---
# PDFの画像認識には 1.5-flash が安定しています
generation_config = {
    "temperature": 0.1,
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 8192,
}

model = genai.GenerativeModel(
    model_name='models/gemini-2.5-flash-lite',
    generation_config=generation_config
)

# Global Variables
UPLOADED_FILES_CACHE = [] 
FILE_LIST_DATA = []

def get_credentials():
    """Get Google Credentials"""
    creds_path = SERVICE_ACCOUNT_FILE
    if not os.path.exists(creds_path):
        creds_path = 'credentials.json'
    
    if not os.path.exists(creds_path):
        print("Warning: credentials.json not found.")
        return None

    scopes = [
        'https://www.googleapis.com/auth/drive.readonly',
        'https://www.googleapis.com/auth/spreadsheets' 
    ]
    return service_account.Credentials.from_service_account_file(creds_path, scopes=scopes)

def load_and_upload_pdfs():
    """Download PDFs from Drive and Upload to Gemini"""
    global UPLOADED_FILES_CACHE, FILE_LIST_DATA
    
    creds = get_credentials()
    if not creds:
        return [], []
    
    service = build('drive', 'v3', credentials=creds)
    
    # Reset
    UPLOADED_FILES_CACHE = []
    FILE_LIST_DATA = []

    try:
        # Get PDF list
        query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/pdf' and trashed=false"
        results = service.files().list(q=query, fields="files(id, name, webViewLink)").execute()
        items = results.get('files', [])

        if not items:
            print("No PDF files found.")
            return [], []

        for item in items:
            print(f"Processing: {item['name']}...")
            
            # Frontend data
            FILE_LIST_DATA.append({
                'name': item['name'],
                'url': item.get('webViewLink', '#')
            })

            # 1. Download to temp file
            request = service.files().get_media(fileId=item['id'])
            
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                downloader = MediaIoBaseDownload(tmp_file, request)
                done = False
                while done is False:
                    _, done = downloader.next_chunk()
                tmp_path = tmp_file.name

            # 2. Upload to Gemini (File API)
            try:
                print(f"Uploading to Gemini: {item['name']}")
                uploaded_file = genai.upload_file(path=tmp_path, display_name=item['name'])
                
                # Wait for processing (CRITICAL for avoiding 403)
                print("Waiting for file processing...")
                while uploaded_file.state.name == "PROCESSING":
                    time.sleep(2) # Wait 2 seconds
                    uploaded_file = genai.get_file(uploaded_file.name)
                
                if uploaded_file.state.name == "FAILED":
                    print(f"File processing failed: {item['name']}")
                    continue

                UPLOADED_FILES_CACHE.append(uploaded_file)
                print(f"Upload Complete: {item['name']}")

            except Exception as upload_error:
                print(f"Upload Error for {item['name']}: {upload_error}")
            finally:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

    except Exception as e:
        print(f"Drive/Upload Error: {e}")
        return [], []

    return UPLOADED_FILES_CACHE, FILE_LIST_DATA

def save_log_to_sheet(user_msg, bot_msg):
    """Save Log"""
    try:
        creds = get_credentials()
        if not creds: return
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sheet.append_row([now, user_msg, bot_msg])
        print("Log saved.")
    except Exception as e:
        print(f"Logging Error: {e}")

# --- Start Up ---
print("System starting... uploading files to Gemini...")
load_and_upload_pdfs()
print("System Ready.")

# --- Routes ---

@app.route('/')
def index():
    return render_template('index.html', files=FILE_LIST_DATA)

@app.route('/refresh')
def refresh_data():
    """Refresh Data"""
    print("Refreshing data...")
    uploaded, file_list = load_and_upload_pdfs()
    
    if file_list:
        return jsonify({
            'status': 'success', 
            'message': 'Data updated successfully.', 
            'files': file_list
        })
    else:
        return jsonify({
            'status': 'error', 
            'message': 'Update failed.'
        })

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message')
    history_list = data.get('history', [])
    
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400

    # History Text
    history_text = ""
    for chat in history_list[-2:]: 
        role = "User" if chat['role'] == 'user' else "AI"
        content = chat['text']
        history_text += f"{role}: {content}\n"

    # System Instruction
    system_instruction = """
    あなたは厳格な事実確認を行う学校の質問応答システムです。
    
    【重要ルール】
    1. 添付された資料(PDF)の内容のみを根拠に回答してください。
    2. 資料内の「グラフ」「表」「地図」「写真」も読み取って回答に活用してください。
    3. 推測や一般論は禁止です。資料にないことは「情報がありません」と答えてください。
    4. 文体は丁寧な「です・ます」調で。
    5. 回答の際は、根拠とした資料の「ファイル名」を明記してください。
    
    [これまでの会話]
    """ + history_text

    # Request Data
    request_content = [system_instruction]
    request_content.extend(UPLOADED_FILES_CACHE)
    request_content.append(f"\n[ユーザーの質問]\n{user_message}")

    try:
        # Generate
        response = model.generate_content(request_content)
        bot_reply = response.text
        
        save_log_to_sheet(user_message, bot_reply)
        
        return jsonify({'reply': bot_reply})
    except Exception as e:
        print(f"Gemini Error: {e}")
        if "429" in str(e):
            return jsonify({'reply': '申し訳ありません。現在アクセスが集中しており（容量制限）、一時的に利用できません。1分ほど待ってから再度お試しください。'})
        return jsonify({'reply': 'エラーが発生しました。'}), 500

if __name__ == '__main__':
    app.run(debug=True)
