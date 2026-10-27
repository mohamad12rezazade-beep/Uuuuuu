import psycopg2

DATABASE_URL = "postgresql://postgres.pqvuttwpkmhjhnccflxi:Ali1365277356@aws-0-ap-south-1.pooler.supabase.com:5432/postgres"

print("در حال تست اتصال به دیتابیس...")
try:
    conn = psycopg2.connect(DATABASE_URL, sslmode='require')
    print("✅ اتصال به دیتابیس موفق بود!")
    conn.close()
except Exception as e:
    print("❌ خطا در اتصال به دیتابیس:")
    print(e)
