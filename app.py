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

# ★親フォルダID
DRIVE_FOLDER_ID = '1fJ3Mbrcw-joAsX33aBu0z4oSQu7I0PhP' 

# ★スプレッドシートID
SPREADSHEET_ID = '1NK0ixXY9hOWuMib22wZxmFX6apUV7EhTDawTXPganZg'

# --- モデル設定 (画像認識・PDF読み込み用) ---
generation_config = {
    "temperature": 0.0,  # ★0.0にして回答のブレと推測を完全に殺します
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 8192,
}

model = genai.GenerativeModel(
    model_name='models/gemini-2.5-flash',
    generation_config=generation_config
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
    """役割ごとのフォルダからPDFを読み込む"""
    global UPLOADED_FILES_CACHE, FILE_LIST_DATA
    
    creds = get_credentials()
    if not creds: return
    
    service = build('drive', 'v3', credentials=creds)
    
    # リセット (フォルダ名に合わせてキーを設定)
    UPLOADED_FILES_CACHE = {'在校生': [], '受験生': [], '保護者': []}
    FILE_LIST_DATA = []
    
    # ★ターゲットフォルダ名 (Googleドライブのフォルダ名と完全一致させる)
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
                
                # フッター表示用データ
                FILE_LIST_DATA.append({
                    'name': item['name'],
                    'url': item.get('webViewLink', '#'),
                    'role': role  # このroleを使ってHTML側で出し分けます
                })

                request = service.files().get_media(fileId=item['id'])
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                    downloader = MediaIoBaseDownload(tmp_file, request)
                    done = False
                    while done is False: _, done = downloader.next_chunk()
                    tmp_path = tmp_file.name

                try:
                    uploaded_file = genai.upload_file(path=tmp_path, display_name=item['name'])
                    # 処理待ち
                    while uploaded_file.state.name == "PROCESSING":
                        time.sleep(2)
                        uploaded_file = genai.get_file(uploaded_file.name)
                    
                    if uploaded_file.state.name == "ACTIVE":
                        role_files.append(uploaded_file)
                        print(f"Upload Complete: {item['name']}")
                except Exception as e:
                    print(f"Upload Error: {e}")
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
    user_role = data.get('role', '在校生') # デフォルトは在校生
    
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400

    history_text = ""
    for chat in history_list[-4:]: 
        role = "User" if chat['role'] == 'user' else "AI"
        content = chat['text']
        history_text += f"{role}: {content}\n"

    target_files = UPLOADED_FILES_CACHE.get(user_role, [])

    # ★ここが重要：役割ごとのペルソナとルール設定
    role_instruction = ""
    if user_role == '在校生':
        role_instruction = "相手は【在校生】です。親しみやすい先輩のような口調で答えてください。"
    elif user_role == '受験生':
        role_instruction = "相手は【受験生】です。優しく歓迎する口調で、学校の魅力を伝えてください。"
    elif user_role == '保護者':
        role_instruction = "相手は【保護者】です。丁寧で信頼感のあるビジネスライクな口調で答えてください。"

    # ★最強のプロンプト：推測禁止とページ数明記を強制
    system_instruction = f"""
    あなたは学校の公式質問応答AIです。
    現在の対話相手設定：{role_instruction}
    
    【回答の絶対ルール】
    1. 添付された資料(PDF)に書かれている内容**のみ**を根拠に回答してください。
    2. あなた自身の知識、一般論、推測を混ぜることは**固く禁止**します。
    3. 資料の中に答えが見つからない場合は、正直に「申し訳ありません、提供された資料の中にはその情報が含まれていません」とだけ答えてください。無理に答えを捏造しないでください。
    4. どのPDFファイルの、どの部分を見て答えたかを示すため、回答の最後には必ず【参照元：ファイル名 (P.ページ数)】を明記してください。
    5. 資料内のグラフや地図、写真の情報も読み取って回答してください。
    
    [これまでの会話]
    """ + history_text

    request_content = [system_instruction]
    if target_files:
        request_content.extend(target_files)
    request_content.append(f"\n[ユーザーの質問]\n{user_message}")

    try:
        response = model.generate_content(request_content)
        bot_reply = response.text
        save_log_to_sheet(user_message, bot_reply, user_role)
        return jsonify({'reply': bot_reply})
    except Exception as e:
        print(f"Gemini Error: {e}")
        return jsonify({'reply': 'エラーが発生しました。'}), 500

if __name__ == '__main__':
    app.run(debug=True)
