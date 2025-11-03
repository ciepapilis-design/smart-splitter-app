import streamlit as st
import pandas as pd
import sqlite3
import json
import os
import requests
from datetime import datetime, timedelta
import pytz 
import psycopg2 
import urllib.parse
import hashlib
import tempfile # ★ 新規追加: 証明書ファイルの一時保存に使用

# --- データベース設定 ---
DB_NAME = 'splitwise_data.db'

# --- API設定とキャッシュ ---
EXCHANGE_RATE_API_URL = "https://open.er-api.com/v6/latest/JPY" 
SUPPORTED_CURRENCIES = ["JPY", "USD", "EUR", "KRW", "TWD", "GBP", "AUD"] 
JST = pytz.timezone('Asia/Tokyo')

# --- DB接続関数 ---

def get_db_connection():
    """PostgreSQLデータベースに接続する (Streamlit Cloudのsecretsを優先)"""
    try:
        if 'DATABASE_URL' in st.secrets:
            db_url = st.secrets['DATABASE_URL']
        elif 'DB_URL' in os.environ:
            db_url = os.environ['DB_URL']
        else:
            st.error("データベースURLが設定されていません。アプリを停止します。")
            st.stop()
            
        # 1. ルート証明書を一時ファイルに書き出す
        root_cert = st.secrets.get('SUPABASE_ROOT_CERT')
        ssl_params = {}
        if root_cert:
            with tempfile.NamedTemporaryFile(mode='w', delete=False) as tmp:
                tmp.write(root_cert)
                cert_path = tmp.name
            
            ssl_params = {
                'sslmode': 'require',
                'sslrootcert': cert_path
            }
        else:
            # Secretsに証明書がない場合、通常のSSL接続を試す
            ssl_params = {'sslmode': 'require'} 

        # 2. URLをパースして接続情報を作成
        parsed_url = urllib.parse.urlparse(db_url)
        
        conn = psycopg2.connect(
            host=parsed_url.hostname,
            database=parsed_url.path[1:],
            user=parsed_url.username,
            password=parsed_url.password,
            port=parsed_url.port or 5432,
            **ssl_params # ★ 修正: SSL接続パラメーターを辞書で展開して渡す
        )
        return conn
    except Exception as e:
        st.error(f"データベース接続エラー: {e}")
        st.stop()


def save_setting(conn, group_id, key, value):
    """設定キーと値をデータベースに保存または更新する"""
    c = conn.cursor()
    c.execute(
        "INSERT INTO settings (group_id, key, value) VALUES (%s, %s, %s) "
        "ON CONFLICT (group_id, key) DO UPDATE SET value = EXCLUDED.value",
        (group_id, key, value)
    )
    conn.commit()

def load_setting(conn, group_id, key, default):
    """設定キーに対応する値をデータベースから読み込む"""
    c = conn.cursor()
    c.execute("SELECT value FROM settings WHERE group_id = %s AND key = %s", (group_id, key))
    result = c.fetchone()
    return result[0] if result else default

def save_event(conn, group_id, event_data):
    """新しいイベントをデータベースに保存する"""
    c = conn.cursor()
    participants_json = json.dumps(event_data['participants'])
    paid_by_json = json.dumps(event_data['paid_by'])
    
    c.execute(
        "INSERT INTO events (group_id, event_name, amount, currency, participants, paid_by) VALUES (%s, %s, %s, %s, %s, %s)",
        (group_id, event_data['event_name'], event_data['amount'], event_data['currency'], participants_json, paid_by_json)
    )
    conn.commit()

def save_person(conn, group_id, person_name):
    """新しい参加者をデータベースに保存する (グループ内で重複しない場合のみ)"""
    c = conn.cursor()
    try:
        c.execute(
            "INSERT INTO people (group_id, person_name) VALUES (%s, %s) ON CONFLICT (group_id, person_name) DO NOTHING", 
            (group_id, person_name)
        )
        conn.commit()
    except Exception as e:
        conn.rollback() 
        pass

def load_data(conn, group_id):
    """データベースから現在のグループのイベントと参加者の両方を読み込む"""
    c = conn.cursor()
    
    # イベントデータの読み込み
    c.execute("SELECT event_name, amount, currency, participants, paid_by FROM events WHERE group_id = %s", (group_id,))
    rows = c.fetchall()
    
    events = []
    for row in rows:
        event_name, amount, currency, participants_json, paid_by_json = row
        participants = json.loads(participants_json)
        paid_by = json.loads(paid_by_json)
        
        events.append({
            'event_name': event_name,
            'amount': amount,
            'currency': currency,
            'participants': participants,
            'paid_by': paid_by
        })
    
    # 参加者データの読み込み
    c.execute("SELECT person_name FROM people WHERE group_id = %s", (group_id,))
    people_rows = c.fetchall()
    all_people = set(row[0] for row in people_rows)

    return events, all_people

def init_db(conn):
    """データベースを初期化し、イベント、参加者、設定テーブルを作成する (group_idを追加)"""
    c = conn.cursor()
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS events (
            id SERIAL PRIMARY KEY,
            group_id TEXT NOT NULL,
            event_name TEXT NOT NULL,
            amount REAL NOT NULL,
            currency TEXT NOT NULL DEFAULT 'JPY',
            participants TEXT NOT NULL,
            paid_by TEXT NOT NULL
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS people (
            id SERIAL PRIMARY KEY,
            group_id TEXT NOT NULL,
            person_name TEXT NOT NULL,
            UNIQUE (group_id, person_name)
        )
    ''')
    
    c.execute('''
        CREATE TABLE IF NOT EXISTS settings (
            group_id TEXT NOT NULL,
            key TEXT NOT NULL,
            value TEXT,
            PRIMARY KEY (group_id, key)
        )
    ''')
    conn.commit()

def get_exchange_rate():
    # (為替レート関数は変更なし)
    now_utc = datetime.now(pytz.utc)
    now_jst = now_utc.astimezone(JST)
    
    if 'rate_cache' in st.session_state:
        cache_time_utc = st.session_state.rate_cache['timestamp']
        if now_utc < cache_time_utc + timedelta(minutes=30):
            cache_time_jst = cache_time_utc.astimezone(JST)
            st.sidebar.info(f"レートはキャッシュから取得されました (取得時刻: {cache_time_jst.strftime('%H:%M:%S')} JST)")
            return st.session_state.rate_cache['rates']

    try:
        response = requests.get(EXCHANGE_RATE_API_URL)
        response.raise_for_status() 
        data = response.json()
        
        rates = data.get('rates', {})
        
        if not rates:
            st.error("APIから為替レートを取得できませんでした。")
            return {"JPY": 1.0}

        st.session_state.rate_cache = {
            'rates': rates,
            'timestamp': now_utc
        }
        st.sidebar.success(f"最新の為替レートを取得しました (時刻: {now_jst.strftime('%H:%M:%S')} JST)")
        return rates

    except requests.exceptions.RequestException as e:
        st.error(f"為替レートAPIへの接続エラー: {e}")
        return {"JPY": 1.0}

def calculate_split(data, rates):
    # (計算ロジック関数は変更なし)
    transactions = []
    
    for event in data:
        amount_float = float(event['amount'])
        
        num_participants = len(event['participants'])
        if num_participants == 0 or amount_float <= 0:
            continue
            
        currency = event['currency']
        
        if currency not in rates or rates[currency] == 0:
            rate_multiplier = 1.0 
        else:
            rate_multiplier = 1.0 / rates[currency]
        
        converted_amount_jpy = amount_float * rate_multiplier
        per_person_share_jpy = converted_amount_jpy / num_participants

        for person in event['participants']:
            paid_amount_foreign = float(event['paid_by'].get(person, 0))
            converted_paid_jpy = paid_amount_foreign * rate_multiplier
            
            transactions.append({
                'person': person,
                'amount_paid': converted_paid_jpy,
                'amount_owed': per_person_share_jpy
            })

    if not transactions:
        return None, []

    df = pd.DataFrame(transactions)
    
    summary = df.groupby('person').agg(
        total_paid=('amount_paid', 'sum'),
        total_owed=('amount_owed', 'sum')
    ).reset_index()

    summary['net_balance'] = summary['total_paid'] - summary['total_owed']

    creditors = summary[summary['net_balance'] > 0].sort_values(by='net_balance', ascending=False)
    debtors = summary[summary['net_balance'] < 0].sort_values(by='net_balance', ascending=True)

    payments = []
    creditors_list = [(row['person'], round(row['net_balance'], 0)) for _, row in creditors.iterrows()]
    debtors_list = [(row['person'], round(abs(row['net_balance']), 0)) for _, row in debtors.iterrows()]

    while creditors_list and debtors_list:
        creditor_name, creditor_amount = creditors_list.pop(0)
        debtor_name, debtor_amount = debtors_list.pop(0)

        payment_amount = min(creditor_amount, debtor_amount)

        payments.append({
            'from': debtor_name,
            'to': creditor_name,
            'amount': payment_amount
        })

        creditor_remaining = creditor_amount - payment_amount
        debtor_remaining = debtor_amount - payment_amount

        if creditor_remaining > 0.01:
            creditors_list.insert(0, (creditor_name, creditor_remaining))
        
        if debtor_remaining > 0.01:
            debtors_list.insert(0, (debtor_name, debtor_remaining))
            
    return summary, payments
    
    
# --- StreamlitによるWebインターフェース構築 ---

st.set_page_config(
    page_title="スマート割り勘計算機", 
    layout="wide", 
    menu_items={'About': 'Python学習で作られた割り勘アプリです。'},
    initial_sidebar_state="expanded"
)
st.title("💰 Smart Splitter (スマート割り勘計算機)")

# 1. 団体IDの取得と設定
GROUP_ID = st.query_params.get("group", "default") 

# 接続と初期化
try:
    conn = get_db_connection()
    init_db(conn)
except:
    st.stop()


# --- アプリケーションのロジック ---

# データをデータベースからロード
loaded_events, loaded_people = load_data(conn, GROUP_ID)

if 'events' not in st.session_state:
    st.session_state.events = loaded_events
if 'all_people' not in st.session_state:
    st.session_state.all_people = loaded_people

db_default_currency = load_setting(conn, GROUP_ID, 'default_currency', 'JPY')
if 'default_currency' not in st.session_state:
    st.session_state.default_currency = db_default_currency

EXCHANGE_RATES = get_exchange_rate()


# --- サイドバー (メンバー管理/設定) ---
with st.sidebar:
    st.header("👥 メンバー管理")
    
    input_key = f"new_person_input"
    new_person = st.text_input("メンバー名", key=input_key)
    
    if st.button("メンバーを追加 ➕", use_container_width=True, type="secondary"):
        person_to_add = st.session_state[input_key].strip()
        
        if person_to_add and person_to_add not in st.session_state.all_people:
            save_person(conn, GROUP_ID, person_to_add) 
            st.session_state.all_people.add(person_to_add)
            st.success(f"'{person_to_add}' を追加しました！")
            st.rerun() 
        elif person_to_add:
            st.warning("その名前は既に追加されているか、空欄です。")
    
    people_list = sorted(list(st.session_state.all_people))
    st.markdown("---")
    
    if people_list:
        st.write("**現在のメンバー:**")
        st.markdown(", ".join(people_list))
    else:
        st.write("**現在のメンバー:** (未登録)")
    
    # デフォルト通貨設定と即時反映ロジック
    st.markdown("---")
    st.subheader("⚙️ アプリ設定")
    
    default_currency_choice = st.selectbox(
        "デフォルトの通貨", 
        SUPPORTED_CURRENCIES, 
        index=SUPPORTED_CURRENCIES.index(st.session_state.default_currency),
        key='default_currency_select'
    )
    
    if st.session_state.default_currency != st.session_state.default_currency_select:
        new_currency = st.session_state.default_currency_select
        st.session_state.default_currency = new_currency
        save_setting(conn, GROUP_ID, 'default_currency', new_currency) # DBに永続化
        st.rerun() 
            
    st.markdown("---")
    st.subheader("🌐 為替レート情報")
    
    display_currencies = ["USD", "EUR", "KRW", "TWD"]
    
    rate_table = {}
    for currency in display_currencies:
        if currency in EXCHANGE_RATES:
            rate_table[currency] = f"{EXCHANGE_RATES[currency]:.5f}" 
            
    st.table(rate_table)

# --- グループ作成・共有機能の追加 ---
st.sidebar.markdown("---")
st.sidebar.header("🔗 グループの共有")

new_group_name = st.sidebar.text_input("新しいグループ名を入力", key="new_group_name_input")

if st.sidebar.button("グループを生成・共有", use_container_width=True, type="primary"):
    if new_group_name:
        # グループ名からユニークなIDを生成 (SHA256ハッシュの先頭8文字を使用)
        unique_id = hashlib.sha256(new_group_name.encode()).hexdigest()[:8]
        
        # Streamlit Cloudの環境変数からホスト名を取得
        host_name = os.environ.get('STREAMLIT_SERVER_ORIGIN', 'https://your-deployed-app.com').split('//')[-1].split(':')[0]
        
        # 共有URLを構築
        share_link = f"https://{host_name}/?group={unique_id}"

        st.sidebar.success(f"グループ '{new_group_name}' が生成されました！")
        
        st.sidebar.markdown("##### 共有リンク")
        st.sidebar.code(share_link)
        st.sidebar.markdown(f"[新しいグループを開く]({share_link})")
    else:
        st.sidebar.warning("グループ名を入力してください。")


# --- メインコンテンツ ---

st.markdown(f"**現在のグループID:** **`{GROUP_ID}`**")


# 新しい支払いイベントの追加フォーム
with st.expander("📝 新しい支払い（立替）を記録する", expanded=True):
    col_name, col_amount, col_currency = st.columns([2, 1, 1])
    
    with col_name:
        event_name = st.text_input("イベント名", value=f"イベント {len(st.session_state.events) + 1}", key="event_name_input")
        
    with col_currency:
        currency = st.selectbox(
            "通貨 💵", 
            SUPPORTED_CURRENCIES, 
            index=SUPPORTED_CURRENCIES.index(st.session_state.default_currency), 
            key='event_currency'
        )

    with col_amount:
        amount = st.number_input(
            f"合計金額 ({st.session_state.event_currency})", 
            min_value=1, 
            step=1, 
            key="amount_input", 
            value=1,
            format="%d"
        )
    
    participants = st.multiselect("👥 参加者 (割り勘の対象者)", people_list, key="participants_select")
    
    st.markdown("##### 💵 誰がいくら立て替えたか")
    
    if 'paid_amounts' not in st.session_state:
        st.session_state.paid_amounts = {}
    
    paid_by = {}
    total_paid = 0

    if participants:
        st.info(f"合計金額 ({st.session_state.event_currency}) になるよう、立て替え額を**整数**で入力してください。")
        for person in participants:
            def update_paid_amount(p=person):
                if st.session_state[f"paid_{p}"] is not None:
                    st.session_state.paid_amounts[p] = int(st.session_state[f"paid_{p}"]) 

            initial_paid_amount = int(st.session_state.paid_amounts.get(person, 0))
            
            paid_amount = st.number_input(
                f"{person} の立て替え額 ({st.session_state.event_currency})", 
                min_value=0,
                step=1,
                key=f"paid_{person}", 
                value=initial_paid_amount,
                on_change=update_paid_amount,
                format="%d"
            )
            paid_by[person] = int(paid_amount)
            total_paid += int(paid_amount)

    # 立替額と合計金額の一致チェック
    col1, col2 = st.columns(2)
    col1.metric("イベント合計金額", f"{st.session_state.amount_input:,.0f} {st.session_state.event_currency}")
    col2.metric("立て替え総額", f"{total_paid:,.0f} {st.session_state.event_currency}")
    
    st.markdown(
        """
        <p style='font-size: 14px; color: gray;'>
        💡 金額入力後、Enterキーを押すか、フィールド外をクリックすると反映されます。
        </p>
        """, 
        unsafe_allow_html=True
    )
    
    is_valid_paid = (total_paid == st.session_state.amount_input) and (st.session_state.amount_input > 0)

    if st.button("イベントを登録 💾", disabled=(not participants or not is_valid_paid), key="register_button", type="primary", use_container_width=True):
        if is_valid_paid:
            event_data = {
                'event_name': st.session_state.event_name_input,
                'amount': st.session_state.amount_input,
                'currency': st.session_state.event_currency,
                'participants': st.session_state.participants_select,
                'paid_by': paid_by
            }
            
            save_event(conn, GROUP_ID, event_data)
            st.session_state.events.append(event_data)
            st.session_state.paid_amounts = {}
            st.success(f"イベント '{event_data['event_name']}' ({event_data['amount']:,.0f} {event_data['currency']}) を登録しました！")
            st.rerun() 
        else:
            st.error(f"エラー: イベント合計金額と立て替え総額が {st.session_state.event_currency} で一致していないか、合計金額がゼロです。")
            
st.markdown("---")

# 登録済みイベントの表示
st.header("📖 登録済み支払いリスト")
if st.session_state.events:
    for event in st.session_state.events:
        currency_symbol = event['currency']
        
        rate_multiplier = 1.0 / EXCHANGE_RATES.get(currency_symbol, 1.0)
        converted_amount = event['amount'] * rate_multiplier
        
        with st.expander(f"**{event['event_name']}** ({event['amount']:,.0f} {currency_symbol})", expanded=False):
            st.markdown(f"**合計金額:** **{event['amount']:,.0f} {currency_symbol}** （現在のレートで**約 {converted_amount:,.0f} 円**）")
            st.markdown(f"**参加者:** {', '.join(event['participants'])}")
            paid_info = ", ".join([f"{p}: {a:,.0f}{currency_symbol}" for p, a in event['paid_by'].items() if a > 0])
            st.markdown(f"**立替者:** {paid_info}")

else:
    st.info("まだ支払いイベントが登録されていません。")

st.markdown("---")

# 最終計算と結果表示
st.header("✅ 精算結果")
if st.session_state.events:
    summary, payments = calculate_split(st.session_state.events, EXCHANGE_RATES)
    
    if summary is not None:
        st.subheader("1. メンバーごとの収支")
        st.info("すべてのイベントを現在のレートで円に換算して計算しています。")
        
        balance_list = []
        for index, row in summary.iterrows():
            person = row['person']
            net_balance = round(row['net_balance'], 0)
            total_paid = round(row['total_paid'], 0)
            total_owed = round(row['total_owed'], 0) 
            
            if net_balance > 0:
                status = f"**{person}** は {total_paid:,.0f} 円を立て替えました（負担すべき額は {total_owed:,.0f} 円）。" \
                         f"**{net_balance:,.0f} 円** **多く払った**ため、返金を受ける必要があります。"
            elif net_balance < 0:
                status = f"**{person}** は {total_paid:,.0f} 円を立て替えましたが（負担すべき額は {total_owed:,.0f} 円）、" \
                         f"**{abs(net_balance):,.0f} 円** **不足しています**（払う必要があります）。"
            else:
                status = f"**{person}** は立て替えと負担が一致しており、精算は不要です。"
            
            balance_list.append(status)
        
        st.markdown('\n'.join([f"- {item}" for item in balance_list]))

        
        st.subheader("2. 最小精算の提案")
        
        if payments:
            payment_list = []
            for payment in payments:
                amount_text = f"{payment['amount']:,.0f} 円"
                payment_list.append(f"**{payment['from']}** が **{payment['to']}** に **{amount_text}** を支払う")
            
            st.success("以下の送金で精算が完了します。")
            st.markdown('\n'.join([f"- {item}" for item in payment_list]))
        else:
            st.info("精算は必要ありません。")
    else:
        st.error("有効なイベントデータがありません。")

st.markdown("---")
# リセットボタン
if st.button(f"現在のグループ ({GROUP_ID}) のデータをリセット 🗑️", type="secondary", use_container_width=True):
    c = conn.cursor()
    c.execute("DELETE FROM events WHERE group_id = %s", (GROUP_ID,))
    c.execute("DELETE FROM people WHERE group_id = %s", (GROUP_ID,))
    c.execute("DELETE FROM settings WHERE group_id = %s", (GROUP_ID,))
    conn.commit()
    st.session_state.events = []
    st.session_state.all_people = set()
    st.success(f"グループID `{GROUP_ID}` の全てのデータがリセットされました。")
    st.rerun()

# 最後にDB接続を閉じる
# conn は Streamlit の実行ごとに開閉されるため、関数外での明示的な close は省略
