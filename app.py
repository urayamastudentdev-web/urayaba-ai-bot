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

# ★ここにGoogleドライブのフォルダIDを貼り付けてください
DRIVE_FOLDER_ID = '1fJ3Mbrcw-joAsX33aBu0z4oSQu7I0PhP' 

# ★ここにスプレッドシートIDを貼り付けてください
SPREADSHEET_ID = '1NK0ixXY9hOWuMib22wZxmFX6apUV7EhTDawTXPganZg'

# --- モデル設定 (画像認識・PDF読み込み用) ---
# 課金済みなら 1.5-flash がPDF認識において最もコスパと性能のバランスが良いです
generation_config = {
    "temperature": 0.1,  # 事実に忠実な回答
    "top_p": 0.95,
    "top_k": 40,
    "max_output_tokens": 8192,
}

model = genai.GenerativeModel(
    model_name='models/gemini-1.5-flash',
    generation_config=generation_config
)

# グローバル変数（アップロード済みファイルオブジェクトを保持）
UPLOADED_FILES_CACHE = [] 
FILE_LIST_DATA = []

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

def load_and_upload_pdfs():
    """Google DriveからPDFをダウンロードし、Geminiへアップロードする(画像認識モード)"""
    global UPLOADED_FILES_CACHE, FILE_LIST_DATA
    
    creds = get_credentials()
    if not creds:
        return [], []
    
    service = build('drive', 'v3', credentials=creds)
    
    # リセット
    UPLOADED_FILES_CACHE = []
    FILE_LIST_DATA = []

    try:
        # DriveからPDF一覧を取得
        query = f"'{DRIVE_FOLDER_ID}' in parents and mimeType='application/pdf' and trashed=false"
        results = service.files().list(q=query, fields="files(id, name, webViewLink)").execute()
        items = results.get('files', [])

        if not items:
            print("No PDF files found.")
            return [], []

        for item in items:
            print(f"Processing: {item['name']}...")
            
            # フロント表示用のリストに追加
            FILE_LIST_DATA.append({
                'name': item['name'],
                'url': item.get('webViewLink', '#')
            })

            # 1. Driveから一時ファイルとしてダウンロード
            request = service.files().get_media(fileId=item['id'])
            
            # 一時ファイルを作成して保存
            with tempfile.NamedTemporaryFile(delete=False, suffix=".pdf") as tmp_file:
                downloader = MediaIoBaseDownload(tmp_file, request)
                done = False
                while done is False:
                    _, done = downloader.next_chunk()
                tmp_path = tmp_file.name

            # 2. Geminiサーバーへアップロード (File API)
            try:
                print(f"Uploading to Gemini: {item['name']}")
                uploaded_file = genai.upload_file(path=tmp_path, display_name=item['name'])
                
                # 403エラー防止：処理完了(ACTIVE)になるまで待機
                print("Waiting for processing...")
                while uploaded_file.state.name == "PROCESSING":
                    time.sleep(2)
                    uploaded_file = genai.get_file(uploaded_file.name)
                
                if uploaded_file.state.name == "FAILED":
                    print(f"Failed to process file: {item['name']}")
                    continue

                # 準備完了したファイルをリストに追加
                UPLOADED_FILES_CACHE.append(uploaded_file)
                print(f"Upload Complete (ACTIVE): {item['name']}")

            except Exception as upload_error:
                print(f"Upload Error for {item['name']}: {upload_error}")
            finally:
                # 一時ファイルは削除
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)

    except Exception as e:
        print(f"Drive/Upload Error: {e}")
        return [], []

    return UPLOADED_FILES_CACHE, FILE_LIST_DATA

def save_log_to_sheet(user_msg, bot_msg, role):
    """ログ保存 (役割も記録)"""
    try:
        creds = get_credentials()
        if not creds: return
        client = gspread.authorize(creds)
        sheet = client.open_by_key(SPREADSHEET_ID).sheet1
        now = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        # 日時, 役割, 質問, 回答
        sheet.append_row([now, role, user_msg, bot_msg])
        print("Log saved.")
    except Exception as e:
        print(f"Logging Error: {e}")

# --- 起動時にファイルを準備 ---
print("System starting... Uploading files to Gemini...")
# 課金済みRenderならタイムアウト時間を延ばせるので、ここで時間をかけても大丈夫です
load_and_upload_pdfs()
print("System Ready.")

# --- ルーティング ---

@app.route('/')
def index():
    return render_template('index.html', files=FILE_LIST_DATA)

@app.route('/refresh')
def refresh_data():
    """知識の更新（再アップロード）"""
    print("Refreshing data...")
    # 課金環境ならここが長くても落ちにくいです
    uploaded, file_list = load_and_upload_pdfs()
    
    if file_list:
        return jsonify({
            'status': 'success', 
            'message': '知識データを更新しました！(ファイル再アップロード完了)', 
            'files': file_list
        })
    else:
        return jsonify({
            'status': 'error', 
            'message': '更新に失敗しました。'
        })

@app.route('/chat', methods=['POST'])
def chat():
    data = request.json
    user_message = data.get('message')
    history_list = data.get('history', [])
    user_role = data.get('role', '在学生') # デフォルトは在学生
    
    if not user_message:
        return jsonify({'error': 'No message provided'}), 400

    # 履歴のテキスト化
    history_text = ""
    # 課金済みなら履歴を多めに送っても大丈夫ですが、安定のため直近4つほど
    for chat in history_list[-4:]: 
        role = "User" if chat['role'] == 'user' else "AI"
        content = chat['text']
        history_text += f"{role}: {content}\n"

    # --- 役割ごとのペルソナ設定 ---
    role_instruction = ""
    if user_role == '在学生':
        role_instruction = """
        ・相手は【在学生】です。
        ・先輩や頼れる先生のような、少しフレンドリーで親身な口調で話してください。
        ・学校生活のルール、行事、手続きなど、内部生に必要な情報を優先してください。
        """
    elif user_role == '受験生':
        role_instruction = """
        ・相手は【受験生（高校生など）】です。
        ・明るく歓迎するような、丁寧で優しい口調で話してください。
        ・入試情報、学校の魅力、キャンパスライフの楽しさをアピールしてください。
        """
    elif user_role == '保護者':
        role_instruction = """
        ・相手は【保護者】です。
        ・非常に丁寧で信頼感のある、ビジネスライクな「です・ます」調で話してください。
        ・学費、就職実績、安全性、サポート体制など、保護者が安心できる情報を優先してください。
        """

    # システムプロンプト
    system_instruction = f"""
    あなたは学校の公式質問応答システムです。
    以下の設定を守って回答してください。

    【相手の属性】
    {role_instruction}
    
    【重要ルール】
    1. 添付された資料(PDF)の内容のみを根拠に回答してください。
    2. 資料内の「グラフ」「表」「地図」「写真」の情報も読み取って回答に活用してください。
    3. [これまでの会話]の流れを考慮して回答してください。
    4. 資料にない補足情報として一般論を混ぜる場合は、必ず「資料には記載がありませんが…」と断りを入れてください。
    5. 根拠とした資料の「ファイル名」と「ページ数」を明記してください。
    
    [これまでの会話]
    """ + history_text

    # リクエストデータの作成
    # [指示, ファイル1, ファイル2..., ユーザーの質問]
    request_content = [system_instruction]
    request_content.extend(UPLOADED_FILES_CACHE) 
    request_content.append(f"\n[ユーザーの質問]\n{user_message}")

    try:
        # 生成実行
        response = model.generate_content(request_content)
        bot_reply = response.text
        
        # ログ保存
        save_log_to_sheet(user_message, bot_reply, user_role)
        
        return jsonify({'reply': bot_reply})
    except Exception as e:
        print(f"Gemini Error: {e}")
        return jsonify({'reply': '申し訳ありません。エラーが発生しました。'}), 500

if __name__ == '__main__':
    app.run(debug=True)
