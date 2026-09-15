import streamlit as st
import psycopg2
import datetime
import pandas as pd
import warnings

# Suppress pandas warning about using raw psycopg2 connections
warnings.filterwarnings('ignore', category=UserWarning)

# Configure the page for mobile-first view
st.set_page_config(page_title="Finance Tracker", layout="centered", initial_sidebar_state="expanded")

# Connect to Supabase via the secrets file
def get_db_connection():
    return psycopg2.connect(st.secrets["DATABASE_URL"])

# --- CLOUD DATABASE INITIALIZATION ---
def init_master_tables():
    conn = get_db_connection()
    c = conn.cursor()
    
    # Create all required tables in the empty cloud database
    c.execute('''CREATE TABLE IF NOT EXISTS members (name TEXT UNIQUE)''')
    c.execute('''CREATE TABLE IF NOT EXISTS categories (name TEXT UNIQUE)''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS transactions (
                 id SERIAL PRIMARY KEY,
                 entry_type TEXT,
                 date DATE,
                 category TEXT,
                 total_amount NUMERIC,
                 is_shared INTEGER)''')
                 
    c.execute('''CREATE TABLE IF NOT EXISTS splits (
                 id SERIAL PRIMARY KEY,
                 transaction_id INTEGER REFERENCES transactions(id) ON DELETE CASCADE,
                 person TEXT,
                 paid_amount NUMERIC,
                 owed_amount NUMERIC)''')
                 
    c.execute('''CREATE TABLE IF NOT EXISTS settlements (
                 id SERIAL PRIMARY KEY,
                 date DATE,
                 person TEXT,
                 amount NUMERIC,
                 settlement_type TEXT)''')
    
    # Seed the default members
    c.execute("SELECT count(*) FROM members")
    if c.fetchone()[0] == 0:
        for m in ["Me", "Wife", "Friend A", "Friend B"]:
            c.execute("INSERT INTO members (name) VALUES (%s) ON CONFLICT DO NOTHING", (m,))
            
    # Seed the default categories
    c.execute("SELECT count(*) FROM categories")
    if c.fetchone()[0] == 0:
        for cat in ["Food", "Travel", "Shopping", "Bills", "Salary", "Other"]:
            c.execute("INSERT INTO categories (name) VALUES (%s) ON CONFLICT DO NOTHING", (cat,))
            
    conn.commit()
    conn.close()

# Run the setup check instantly
init_master_tables()

# Fetch the live master lists from the database
def get_master_list(table_name):
    conn = get_db_connection()
    df = pd.read_sql_query(f"SELECT name FROM {table_name} ORDER BY name", conn)
    conn.close()
    return df['name'].tolist()

MEMBER_LIST = get_master_list('members')
if "Me" in MEMBER_LIST:
    MEMBER_LIST.insert(0, MEMBER_LIST.pop(MEMBER_LIST.index("Me")))
    
CATEGORY_LIST = get_master_list('categories')

# --- DATABASE FETCH FUNCTIONS ---
def get_financial_summary():
    conn = get_db_connection()
    c = conn.cursor()
    
    c.execute("SELECT SUM(total_amount) FROM transactions WHERE entry_type='Income' AND is_shared=0")
    personal_income = c.fetchone()[0] or 0.0
    
    c.execute('''SELECT SUM(s.owed_amount) FROM splits s
                 JOIN transactions t ON s.transaction_id = t.id
                 WHERE t.entry_type='Income' AND s.person='Me' ''')
    shared_income = c.fetchone()[0] or 0.0
    income = float(personal_income) + float(shared_income)
    
    c.execute("SELECT SUM(total_amount) FROM transactions WHERE entry_type='Expense' AND is_shared=0")
    personal_expense = c.fetchone()[0] or 0.0
    
    c.execute('''SELECT SUM(s.owed_amount) FROM splits s
                 JOIN transactions t ON s.transaction_id = t.id
                 WHERE t.entry_type='Expense' AND s.person='Me' ''')
    shared_expense = c.fetchone()[0] or 0.0
    actual_expense = float(personal_expense) + float(shared_expense)
    
    savings = income - actual_expense
    conn.close()
    return income, actual_expense, savings

def get_member_balances():
    conn = get_db_connection()
    c = conn.cursor()
    
    balances = {}
    total_receivable = 0.0
    total_payable = 0.0
    
    for person in [m for m in MEMBER_LIST if m != "Me"]:
        c.execute('''SELECT SUM(s2.owed_amount) FROM splits s1 
                     JOIN splits s2 ON s1.transaction_id = s2.transaction_id 
                     JOIN transactions t ON s1.transaction_id = t.id
                     WHERE t.entry_type='Expense' AND s1.person='Me' AND s1.paid_amount>0 AND s2.person=%s''', (person,))
        exp_they_owe_me = c.fetchone()[0] or 0.0
        
        c.execute('''SELECT SUM(s2.owed_amount) FROM splits s1 
                     JOIN splits s2 ON s1.transaction_id = s2.transaction_id 
                     JOIN transactions t ON s1.transaction_id = t.id
                     WHERE t.entry_type='Expense' AND s1.person=%s AND s1.paid_amount>0 AND s2.person='Me' ''', (person,))
        exp_i_owe_them = c.fetchone()[0] or 0.0

        c.execute('''SELECT SUM(s2.owed_amount) FROM splits s1 
                     JOIN splits s2 ON s1.transaction_id = s2.transaction_id 
                     JOIN transactions t ON s1.transaction_id = t.id
                     WHERE t.entry_type='Income' AND s1.person=%s AND s1.paid_amount>0 AND s2.person='Me' ''', (person,))
        inc_they_owe_me = c.fetchone()[0] or 0.0
        
        c.execute('''SELECT SUM(s2.owed_amount) FROM splits s1 
                     JOIN splits s2 ON s1.transaction_id = s2.transaction_id 
                     JOIN transactions t ON s1.transaction_id = t.id
                     WHERE t.entry_type='Income' AND s1.person='Me' AND s1.paid_amount>0 AND s2.person=%s''', (person,))
        inc_i_owe_them = c.fetchone()[0] or 0.0
        
        c.execute("SELECT SUM(amount) FROM settlements WHERE person=%s AND settlement_type='Amount Received'", (person,))
        settled_to_me = c.fetchone()[0] or 0.0
        
        c.execute("SELECT SUM(amount) FROM settlements WHERE person=%s AND settlement_type='Amount Paid'", (person,))
        settled_to_them = c.fetchone()[0] or 0.0
        
        they_owe_me = float(exp_they_owe_me) + float(inc_they_owe_me)
        i_owe_them = float(exp_i_owe_them) + float(inc_i_owe_them)
        
        net_balance = (they_owe_me - float(settled_to_me)) - (i_owe_them - float(settled_to_them))
        balances[person] = net_balance
        
        if net_balance > 0:
            total_receivable += net_balance
        elif net_balance < 0:
            total_payable += abs(net_balance)
            
    conn.close()
    return balances, total_receivable, total_payable

# --- DATABASE SAVE FUNCTIONS ---
def save_transaction(entry_type, date, category, amount, is_shared, payer=None, split_amounts=None):
    conn = get_db_connection()
    c = conn.cursor()
    # PostgreSQL requires RETURNING id to fetch the last inserted row
    c.execute('''INSERT INTO transactions (entry_type, date, category, total_amount, is_shared)
                 VALUES (%s, %s, %s, %s, %s) RETURNING id''', 
              (entry_type, str(date), category, amount, 1 if is_shared else 0))
    
    transaction_id = c.fetchone()[0]
    
    if is_shared and split_amounts:
        involved_people = set(split_amounts.keys())
        involved_people.add(payer)
        for person in involved_people:
            paid = amount if person == payer else 0.0
            owed = split_amounts.get(person, 0.0)
            c.execute('''INSERT INTO splits (transaction_id, person, paid_amount, owed_amount)
                         VALUES (%s, %s, %s, %s)''', (transaction_id, person, paid, owed))
    conn.commit()
    conn.close()

def save_settlement(date, person, amount, settlement_type):
    conn = get_db_connection()
    c = conn.cursor()
    c.execute('''INSERT INTO settlements (date, person, amount, settlement_type)
                 VALUES (%s, %s, %s, %s)''', (str(date), person, amount, settlement_type))
    conn.commit()
    conn.close()

# --- NAVIGATION ---
st.sidebar.title("Navigation")
page = st.sidebar.radio("Go to", ["Dashboard", "Add Entry", "Settlement", "Reports", "Manage Master"])

# --- SCREEN 1: DASHBOARD ---
if page == "Dashboard":
    st.title("Dashboard")
    
    income, actual_expense, savings = get_financial_summary()
    balances, total_receivable, total_payable = get_member_balances()
    net_position = savings + total_receivable - total_payable
    
    col1, col2 = st.columns(2)
    col1.metric("Income", f"₹{income:,.2f}")
    col1.metric("Savings", f"₹{savings:,.2f}")
    col1.metric("Receivable", f"₹{total_receivable:,.2f}")
    
    col2.metric("Actual Expense", f"₹{actual_expense:,.2f}")
    col2.metric("Net Position", f"₹{net_position:,.2f}")
    col2.metric("Payable", f"₹{total_payable:,.2f}")

    st.markdown("---")
    st.subheader("Member Balances")
    
    for person, bal in balances.items():
        if bal > 0:
            st.write(f"**{person}:** +₹{bal:,.2f} *(They owe you)*")
        elif bal < 0:
            st.write(f"**{person}:** -₹{abs(bal):,.2f} *(You owe them)*")
        else:
            st.write(f"**{person}:** ₹0.00")

# --- SCREEN 2: ADD ENTRY ---
elif page == "Add Entry":
    st.title("Add New Entry")
    
    entry_type = st.radio("Transaction Type", ["Expense", "Income"], horizontal=True)
    entry_date = st.date_input("Date")
    category = st.selectbox("Category", CATEGORY_LIST)
    amount = st.number_input("Amount (₹)", min_value=0.0, step=100.0)
    
    is_shared = st.checkbox("Shared Expense")
    split_amounts = {}
    payer = "Me"
    
    if is_shared:
        st.markdown("---")
        st.subheader("Split Details")
        
        payer_label = "Who Paid?" if entry_type == "Expense" else "Who Received?"
        payer = st.selectbox(payer_label, MEMBER_LIST)
        
        participants = st.multiselect("Participants", MEMBER_LIST, default=MEMBER_LIST)
        
        if len(participants) > 0:
            split_method = st.radio("How to split?", ["Equally", "Custom Amounts"], horizontal=True)
            equal_share = round(amount / len(participants), 2)
            
            if split_method == "Equally":
                st.info(f"Auto-split: ₹{equal_share} per person")
                for person in participants:
                    split_amounts[person] = equal_share
            else:
                st.write("Edit Custom Amounts:")
                for person in participants:
                    split_amounts[person] = st.number_input(f"{person}'s Share", value=equal_share, step=10.0)
    
    if st.button("Save Entry"):
        if amount <= 0:
            st.error("Amount must be greater than 0.")
        else:
            if is_shared:
                total_split = sum(split_amounts.values())
                if abs(total_split - amount) > 1.0:
                    st.error(f"Error: Your split amounts (₹{total_split}) do not match the total (₹{amount}).")
                else:
                    save_transaction(entry_type, entry_date, category, amount, True, payer, split_amounts)
                    st.success("Shared entry saved successfully!")
            else:
                save_transaction(entry_type, entry_date, category, amount, False)
                st.success("Personal entry saved successfully!")

# --- SCREEN 3: SETTLEMENT ---
elif page == "Settlement":
    st.title("Settle Balances")
    
    with st.form("settlement_form"):
        person = st.selectbox("Person", [m for m in MEMBER_LIST if m != "Me"])
        settle_amount = st.number_input("Amount (₹)", min_value=0.0, step=100.0)
        settle_type = st.radio("Settlement Type", ["Amount Received", "Amount Paid"], horizontal=True)
        settle_date = st.date_input("Date", value=datetime.date.today())
        
        submitted = st.form_submit_button("Save Settlement")
        if submitted:
            if settle_amount > 0:
                save_settlement(settle_date, person, settle_amount, settle_type)
                st.success(f"Settlement of ₹{settle_amount} with {person} saved to database!")
            else:
                st.error("Please enter a valid amount.")

# --- SCREEN 4: REPORTS ---
elif page == "Reports":
    st.title("Reports & Analytics")
    conn = get_db_connection()
    
    st.subheader("Party Ledger Statement")
    selected_party = st.selectbox("Select Party to view their detailed statement:", [m for m in MEMBER_LIST if m != "Me"])
    
    if selected_party:
        c = conn.cursor()
        c.execute('''SELECT SUM(s2.owed_amount) FROM splits s1 
                     JOIN splits s2 ON s1.transaction_id = s2.transaction_id 
                     JOIN transactions t ON s1.transaction_id = t.id
                     WHERE t.entry_type='Expense' AND s1.person='Me' AND s1.paid_amount>0 AND s2.person=%s''', (selected_party,))
        exp_receivable = c.fetchone()[0] or 0.0
        
        c.execute('''SELECT SUM(s2.owed_amount) FROM splits s1 
                     JOIN splits s2 ON s1.transaction_id = s2.transaction_id 
                     JOIN transactions t ON s1.transaction_id = t.id
                     WHERE t.entry_type='Income' AND s1.person=%s AND s1.paid_amount>0 AND s2.person='Me' ''', (selected_party,))
        inc_receivable = c.fetchone()[0] or 0.0
        total_receivable = float(exp_receivable) + float(inc_receivable)
        
        c.execute('''SELECT SUM(s2.owed_amount) FROM splits s1 
                     JOIN splits s2 ON s1.transaction_id = s2.transaction_id 
                     JOIN transactions t ON s1.transaction_id = t.id
                     WHERE t.entry_type='Expense' AND s1.person=%s AND s1.paid_amount>0 AND s2.person='Me' ''', (selected_party,))
        exp_payable = c.fetchone()[0] or 0.0
        
        c.execute('''SELECT SUM(s2.owed_amount) FROM splits s1 
                     JOIN splits s2 ON s1.transaction_id = s2.transaction_id 
                     JOIN transactions t ON s1.transaction_id = t.id
                     WHERE t.entry_type='Income' AND s1.person='Me' AND s1.paid_amount>0 AND s2.person=%s''', (selected_party,))
        inc_payable = c.fetchone()[0] or 0.0
        total_payable = float(exp_payable) + float(inc_payable)
        
        c.execute("SELECT SUM(amount) FROM settlements WHERE person=%s AND settlement_type='Amount Received'", (selected_party,))
        settled_received = c.fetchone()[0] or 0.0
        
        c.execute("SELECT SUM(amount) FROM settlements WHERE person=%s AND settlement_type='Amount Paid'", (selected_party,))
        settled_paid = c.fetchone()[0] or 0.0
        
        net_total = (total_receivable - float(settled_received)) - (total_payable - float(settled_paid))
        
        st.markdown(f"**Financial Summary: {selected_party}**")
        col1, col2, col3 = st.columns(3)
        col1.metric("Gross Receivable", f"₹{total_receivable:,.2f}")
        col1.metric("Settlements Received", f"₹{float(settled_received):,.2f}")
        
        col2.metric("Gross Payable", f"₹{total_payable:,.2f}")
        col2.metric("Settlements Paid", f"₹{float(settled_paid):,.2f}")
        
        delta_status = "They owe you" if net_total > 0 else "You owe them" if net_total < 0 else "Fully Settled"
        col3.metric("Net Balance", f"₹{abs(net_total):,.2f}", delta=delta_status, delta_color="normal" if net_total >= 0 else "inverse")
        
        st.markdown("---")
        
        st.write(f"**Shared Transactions involving {selected_party}**")
        party_query = '''
            SELECT t.date as Date, t.entry_type as Type, t.category as Category, 
                   t.total_amount as "Total Bill", s.paid_amount as "They Paid", s.owed_amount as "Their Share"
            FROM splits s
            JOIN transactions t ON s.transaction_id = t.id
            WHERE s.person = %s
        '''
        df_party = pd.read_sql_query(party_query, conn, params=(selected_party,))
        if not df_party.empty:
            st.dataframe(df_party, use_container_width=True, hide_index=True)
        else:
            st.info(f"No shared transactions found for {selected_party}.")
        
        st.write(f"**Settlements logged with {selected_party}**")
        settlement_query = '''
            SELECT date as Date, settlement_type as Type, amount as Amount 
            FROM settlements WHERE person = %s
        '''
        df_settlements = pd.read_sql_query(settlement_query, conn, params=(selected_party,))
        if not df_settlements.empty:
            st.dataframe(df_settlements, use_container_width=True, hide_index=True)
        else:
            st.info(f"No settlements found for {selected_party}.")

    st.markdown("---")
    st.subheader("Raw Data & Exports")
    
    df_all = pd.read_sql_query("SELECT * FROM transactions", conn)
    if not df_all.empty:
        st.download_button(
            label="Download Master Journal (CSV)",
            data=df_all.to_csv(index=False).encode('utf-8'),
            file_name=f"master_journal_{datetime.date.today()}.csv",
            mime="text/csv",
        )
        with st.expander("Preview Master Journal"):
            st.dataframe(df_all, use_container_width=True, hide_index=True)
            
    export_splits_query = '''
        SELECT t.date, t.entry_type, t.category, t.total_amount, 
               s.person as Party, s.paid_amount, s.owed_amount
        FROM splits s
        JOIN transactions t ON s.transaction_id = t.id
    '''
    df_splits = pd.read_sql_query(export_splits_query, conn)
    if not df_splits.empty:
        st.download_button(
            label="Download Party Subledger (CSV)",
            data=df_splits.to_csv(index=False).encode('utf-8'),
            file_name=f"party_subledger_{datetime.date.today()}.csv",
            mime="text/csv",
        )
        with st.expander("Preview Party Subledger"):
            st.dataframe(df_splits, use_container_width=True, hide_index=True)

    conn.close()

# --- SCREEN 5: MANAGE MASTER ---
elif page == "Manage Master":
    st.title("Manage Master Data")
    master_type = st.radio("Select Master to Manage:", ["Members", "Categories"], horizontal=True)
    st.markdown("---")
    
    if master_type == "Members":
        st.subheader("Add New Member")
        with st.form("add_member"):
            new_member = st.text_input("Name")
            if st.form_submit_button("Add Member"):
                if new_member:
                    try:
                        conn = get_db_connection()
                        conn.execute("INSERT INTO members (name) VALUES (%s)", (new_member,))
                        conn.commit()
                        conn.close()
                        st.success(f"Added {new_member}!")
                        st.rerun()
                    except psycopg2.IntegrityError:
                        st.error("This member already exists.")
        
        st.subheader("Remove Member")
        deletable_members = [m for m in MEMBER_LIST if m != "Me"]
        if deletable_members:
            with st.form("delete_member"):
                del_member = st.selectbox("Select Member", deletable_members)
                st.warning("Note: Past transactions remain intact.")
                if st.form_submit_button("Remove Member"):
                    conn = get_db_connection()
                    conn.execute("DELETE FROM members WHERE name=%s", (del_member,))
                    conn.commit()
                    conn.close()
                    st.success(f"Removed {del_member}!")
                    st.rerun()

    elif master_type == "Categories":
        st.subheader("Add New Category")
        with st.form("add_category"):
            new_cat = st.text_input("Category Name")
            if st.form_submit_button("Add Category"):
                if new_cat:
                    try:
                        conn = get_db_connection()
                        conn.execute("INSERT INTO categories (name) VALUES (%s)", (new_cat,))
                        conn.commit()
                        conn.close()
                        st.success(f"Added {new_cat}!")
                        st.rerun()
                    except psycopg2.IntegrityError:
                        st.error("This category already exists.")
        
        st.subheader("Remove Category")
        if CATEGORY_LIST:
            with st.form("delete_category"):
                del_cat = st.selectbox("Select Category", CATEGORY_LIST)
                if st.form_submit_button("Remove Category"):
                    conn = get_db_connection()
                    conn.execute("DELETE FROM categories WHERE name=%s", (del_cat,))
                    conn.commit()
                    conn.close()
                    st.success(f"Removed {del_cat}!")
                    st.rerun()