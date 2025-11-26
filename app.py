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

# ★ここに「親フォルダ」のIDを貼り付けてください
# (このフォルダの中に「在学生」「受験生」「保護者」というフォルダを作ってください)
DRIVE_FOLDER_ID = '1fJ3Mbrcw-joAsX33aBu0z4oSQu7I0PhP' 

# ★スプレッドシートID
SPREADSHEET_ID = '1NK0ixXY9hOWuMib22wZxmFX6apUV7EhTDawTXPganZg'

# --- モデル設定 (画像認識・PDF読み込み用) ---
generation_config = {
    "temperature": 0.1,
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 8192,
}

model = genai.GenerativeModel(
    model_name='models/gemini-1.5-flash',
    generation_config=generation_config
)

# グローバル変数（役割ごとのファイルキャッシュ）
UPLOADED_FILES_CACHE = {}  # {'在学生': [], '受験生': [], '保護者': []}
FILE_LIST_DATA = []        # 全ファイル一覧（フッター表示用）

def get_credentials():
    """認証情報を取得"""
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

def load_and_upload_pdfs_by_role():
    """
    Drive内の「在学生」「受験生」「保護者」フォルダを探し、
    それぞれのPDFをGeminiへアップロードして振り分ける
    """
    global UPLOADED_FILES_CACHE, FILE_LIST_DATA
    
    creds = get_credentials()
    if not creds: return
    
    service = build('drive', 'v3', credentials=creds)
    
    # リセット
    UPLOADED_FILES_CACHE = {'在学生': [], '受験生': [], '保護者': []}
    FILE_LIST_DATA = []
    
    # 探すフォルダ名リスト
    target_roles = ['在学生', '受験生', '保護者']

    try:
        for role in target_roles:
            print(f"--- Searching folder for: {role} ---")
            
            # 1. 役割名のフォルダIDを探す
            query_folder = f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/vnd.google-apps.folder' and name='{role}' and trashed=false"
            results = service.files().list(q=query_folder, fields="files(id, name)").execute()
            folders = results.get('files', [])
            
            if not folders:
                print(f"Folder '{role}' not found.")
                continue
                
            folder_id = folders[0]['id']
            print(f"Found folder: {role} (ID: {folder_id})")

            # 2. そのフォルダ内のPDFを取得
            query_files = f"'{folder_id}' in parents and mimeType='application/pdf' and trashed=false"
            res_files = service.files().list(q=query_files, fields="files(id, name, webViewLink)").execute()
            items = res_files.get('files', [])
            
            if not items:
                print(f"No PDFs in '{role}' folder.")
                continue

            role_files = []

            for item in items:
                print(f"Processing [{role}]: {item['name']}...")
                
                # フッター表示用（分かりやすいように【役割】をつける）
                FILE_LIST_DATA.append({
                    'name': f"【{role}】{item['name']}",
                    'url': item.get('webViewLink', '#')
                })

                # ダウンロード & アップロード処理
                request = service.files().get_media(fileId=item['id'])
                
                with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                    downloader = MediaIoBaseDownload(tmp_file, request)
                    done = False
                    while done is False:
                        _, done = downloader.next_chunk()
                    tmp_path = tmp_file.name

                try:
                    # Geminiへアップロード
                    uploaded_file = genai.upload_file(path=tmp_path, display_name=item['name'])
                    
                    # 処理待ち
                    while uploaded_file.state.name == "PROCESSING":
                        time.sleep(1)
                        uploaded_file = genai.get_file(uploaded_file.name)
                    
                    if uploaded_file.state.name == "ACTIVE":
                        role_files.append(uploaded_file)
                        print(f"Upload Complete: {item['name']}")
                    else:
                        print(f"Upload Failed: {item['name']}")

                except Exception as upload_error:
                    print(f"Upload Error: {upload_error}")
                finally:
                    if os.path.exists(tmp_path):
                        os.remove(tmp_path)
            
            # 役割ごとのキャッシュに保存
            UPLOADED_FILES_CACHE[role] = role_files

    except Exception as e:
        print(f"Drive Process Error: {e}")

    return UPLOADED_FILES_CACHE, FILE_LIST_DATA

def save_log_to_sheet(user_msg, bot_msg, role):
    """ログ保存"""
    try:
        creds = get_credentials()
        if not creds: return
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        sheet.append_row([now, role, user_msg, bot_msg])
        print("Log saved.")
    except Exception as e:
        print(f"Logging Error: {e}")

# --- 起動時 ---
print("System starting... Uploading files by role...")
load_and_upload_pdfs_by_role()
print("System Ready.")

# --- Routing ---

@app.route('/')
def index():
    return render_template('index.html', files=FILE_LIST_DATA)

@app.route('/refresh')
def refresh_data():
    """知識の更新"""
    print("Refreshing data...")
    load_and_upload_pdfs_by_role()
    return jsonify({
        'status': 'success', 
        'message': '知識データを更新しました！', 
        'files': FILE_LIST_DATA
    })

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message')
    history_list = data.get('history', [])
    user_role = data.get('role', '在学生') # フロントから役割を受け取る
    
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400

    # 履歴テキスト化
    history_text = ""
    for chat in history_list[-4:]: 
        role = "User" if chat['role'] == 'user' else "AI"
        content = chat['text']
        history_text += f"{role}: {content}\n"

    # ★役割に応じたファイルを取得
    target_files = UPLOADED_FILES_CACHE.get(user_role, [])

    # 役割ごとの振る舞い設定
    role_instruction = ""
    if user_role == '在学生':
        role_instruction = "相手は【在学生】です。先輩や先生のような親しみやすい口調で、学校生活のルールや行事について詳しく答えてください。"
    elif user_role == '受験生':
        role_instruction = "相手は【受験生】です。優しく歓迎するような口調で、入試情報や学校の魅力をアピールしてください。"
    elif user_role == '保護者':
        role_instruction = "相手は【保護者】です。丁寧で信頼感のある「です・ます」調で、学費や就職実績、サポート体制について答えてください。"

    # システムプロンプト
    system_instruction = f"""
    あなたは学校の公式質問応答システムです。
    
    【現在の設定】
    {role_instruction}
    
    【重要ルール】
    1. 添付された資料(PDF)の内容のみを根拠に回答してください。
    2. 資料内の「グラフ」「表」「地図」「写真」の情報も読み取って活用してください。
    3. 推測や一般論を混ぜる場合は「資料にはありませんが…」と断りを入れてください。
    4. 根拠とした資料名とページ数を明記してください。
    
    [これまでの会話]
    """ + history_text

    # AIに渡すデータ：[指示, (役割に合ったファイル達), 質問]
    request_content = [system_instruction]
    
    if target_files:
        request_content.extend(target_files)
    else:
        print(f"Warning: No files found for role {user_role}")
        # ファイルがない場合でも会話だけは成立させる
    
    request_content.append(f"\n[ユーザーの質問]\n{user_message}")

    try:
        # 生成実行
        response = model.generate_content(request_content)
        bot_reply = response.text
        
        save_log_to_sheet(user_message, bot_reply, user_role)
        
        return jsonify({'reply': bot_reply})
    except Exception as e:
        print(f"Gemini Error: {e}")
        return jsonify({'reply': 'エラーが発生しました。'}), 500

if __name__ == '__main__':
    app.run(debug=True)
