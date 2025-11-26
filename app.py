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

# --- 設定エリア ---
GENAI_API_KEY = os.environ.get("GEMINI_API_KEY")
genai.configure(api_key=GENAI_API_KEY)

SERVICE_ACCOUNT_FILE = '/etc/secrets/credentials.json'
DRIVE_FOLDER_ID = '1fJ3Mbrcw-joAsX33aBu0z4oSQu7I0PhP' 
SPREADSHEET_ID = '1NK0ixXY9hOWuMib22wZxmFX6apUV7EhTDawTXPganZg'

# --- モデル設定 ---
generation_config = {
    "temperature": 0.1,
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 8192,
}

# 安全フィルター解除（誤検知防止）
safety_settings = [
    {"category": "HARM_CATEGORY_HARASSMENT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_HATE_SPEECH", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_SEXUALLY_EXPLICIT", "threshold": "BLOCK_NONE"},
    {"category": "HARM_CATEGORY_DANGEROUS_CONTENT", "threshold": "BLOCK_NONE"},
]

model = genai.GenerativeModel(
    model_name='models/gemini-2.5-flash',
    generation_config=generation_config,
    safety_settings=safety_settings
)

# グローバル変数
UPLOADED_FILES_CACHE = {'在校生': [], '受験生': [], '保護者': []}
FILE_LIST_DATA = []

def get_credentials():
    """認証情報を取得"""
    creds_path = SERVICE_ACCOUNT_FILE
    if not os.path.exists(creds_path):
        creds_path = 'credentials.json'
    if not os.path.exists(creds_path): return None
    scopes = ['https://www.googleapis.com/auth/drive.readonly', 'https://www.googleapis.com/auth/spreadsheets']
    return service_account.Credentials.from_service_account_file(creds_path, scopes=scopes)

def load_and_upload_pdfs_by_role():
    """役割ごとのフォルダからPDFを読み込み、アップロードする"""
    global UPLOADED_FILES_CACHE, FILE_LIST_DATA
    
    creds = get_credentials()
    if not creds: return
    
    service = build('drive', 'v3', credentials=creds)
    
    # リセット
    UPLOADED_FILES_CACHE = {'在校生': [], '受験生': [], '保護者': []}
    FILE_LIST_DATA = []
    
    target_roles = ['在校生', '受験生', '保護者']

    try:
        for role in target_roles:
            print(f"--- Searching folder for: {role} ---")
            
            query_folder = f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and name='{role}' and trashed=false"
            results = service.files().list(q=query_folder, fields="files(id, name)").execute()
            folders = results.get('files', [])
            
            if not folders:
                print(f"Folder '{role}' not found.")
                continue
                
            folder_id = folders[0]['id']
            
            query_files = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
            res_files = service.files().list(q=query_files, fields="files(id, name, webViewLink)").execute()
            items = res_files.get('files', [])
            
            if not items: continue

            role_files = []

            for item in items:
                print(f"Processing [{role}]: {item['name']}...")
                
                FILE_LIST_DATA.append({
                    'name': item['name'],
                    'url': item.get('webViewLink', '#'),
                    'role': role 
                })

                request = service.files().get_media(fileId=item['id'])
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                    downloader = MediaIoBaseDownload(tmp_file, request)
                    done = False
                    while done is False: _, done = downloader.next_chunk()
                    tmp_path = tmp_file.name

                try:
                    uploaded_file = genai.upload_file(path=tmp_path, display_name=item['name'])
                    
                    retry_count = 0
                    while uploaded_file.state.name == "PROCESSING" and retry_count < 30:
                        time.sleep(2)
                        uploaded_file = genai.get_file(uploaded_file.name)
                        retry_count += 1
                    
                    if uploaded_file.state.name == "ACTIVE":
                        role_files.append(uploaded_file)
                        print(f"Upload Complete: {item['name']}")
                    else:
                        print(f"Upload Failed: {item['name']}")

                except Exception as upload_error:
                    print(f"Upload Error: {upload_error}")
                finally:
                    if os.path.exists(tmp_path): os.remove(tmp_path)
            
            UPLOADED_FILES_CACHE[role] = role_files

    except Exception as e:
        print(f"Drive Process Error: {e}")

    return UPLOADED_FILES_CACHE, FILE_LIST_DATA

def save_log_to_sheet(user_msg, bot_msg, role):
    try:
        creds = get_credentials()
        if not creds: return
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sheet.append_row([now, role, user_msg, bot_msg])
    except Exception as e:
        print(f"Logging Error: {e}")

# 起動時
print("System starting... Uploading files by role...")
load_and_upload_pdfs_by_role()
print("System Ready.")

@app.route('/')
def index():
    return render_template('index.html', files=FILE_LIST_DATA)

@app.route('/refresh')
def refresh_data():
    print("Refreshing data...")
    load_and_upload_pdfs_by_role()
    return jsonify({'status': 'success', 'message': '更新完了', 'files': FILE_LIST_DATA})

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message')
    history_list = data.get('history', [])
    user_role = data.get('role', '在校生')
    
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400

    history_text = ""
    for chat in history_list[-4:]: 
        role = "User" if chat['role'] == 'user' else "AI"
        content = chat['text']
        history_text += f"{role}: {content}\n"

    # 対象ファイルを取得
    target_files = UPLOADED_FILES_CACHE.get(user_role, [])

    # ★追加：AIに「今持っているファイル情報」を言葉で教える処理
    if target_files:
        # ファイル名リストを作成
        file_names_str = "\n".join([f"・{f.display_name}" for f in target_files])
        file_count_info = f"あなたは現在、以下の【合計{len(target_files)}つ】のファイルを資料として持っています：\n{file_names_str}"
    else:
        file_count_info = "現在、参照できる資料ファイルはありません。"

    # ペルソナ設定
    role_instruction = ""
    if user_role == '在校生':
        role_instruction = "相手は【在校生】です。先輩や先生のような親しみやすい口調で答えてください。"
    elif user_role == '受験生':
        role_instruction = "相手は【受験生】です。優しく歓迎するような口調で答えてください。"
    elif user_role == '保護者':
        role_instruction = "相手は【保護者】です。丁寧で信頼感のある口調で答えてください。"

    # プロンプト（ファイル情報を組み込む）
    system_instruction = f"""
    あなたは学校の公式質問応答AIです。
    現在の対話相手設定：{role_instruction}
    
    【参照資料の状況】
    {file_count_info}
    
    【回答の絶対ルール】
    1. 添付された資料(PDF)の内容のみを根拠に回答してください。
    2. ユーザーから「どんなファイルを見ていますか？」「資料は何個ありますか？」と聞かれた場合は、上記の【参照資料の状況】の情報をそのまま伝えてください。
    3. 資料内のグラフ、地図、写真の情報も読み取って回答してください。
    4. あなた自身の知識や推測は混ぜないでください。
    5. 回答の最後には必ず【参照元：ファイル名 (P.ページ数)】を明記してください。
    
    [これまでの会話]
    """ + history_text

    request_content = [system_instruction]
    if target_files:
        request_content.extend(target_files)
    request_content.append(f"\n[ユーザーの質問]\n{user_message}")

    try:
        response = model.generate_content(request_content)
        try:
            bot_reply = response.text
        except ValueError:
            print(f"Blocked: {response.prompt_feedback}")
            bot_reply = "申し訳ありません。安全フィルターにより回答が生成できませんでした。"

        save_log_to_sheet(user_message, bot_reply, user_role)
        return jsonify({'reply': bot_reply})

    except Exception as e:
        print(f"Gemini Error: {e}")
        return jsonify({'reply': 'エラーが発生しました。'}), 500

if __name__ == '__main__':
    app.run(debug=True)
