"""Run once:  python check_setup.py   — diagnoses signup/500 problems. Read-only; changes nothing."""
import os, json, base64
from dotenv import load_dotenv
load_dotenv()
url, key, jwt_secret = os.environ.get("SUPABASE_URL"), os.environ.get("SUPABASE_SERVICE_KEY"), os.environ.get("JWT_SECRET")
print("SUPABASE_URL set:", bool(url)); print("JWT_SECRET set:", bool(jwt_secret))
try:
    role = json.loads(base64.urlsafe_b64decode(key.split(".")[1] + "==")).get("role")
    print("Key role:", role, "" if role == "service_role" else "  <-- WRONG: SUPABASE_SERVICE_KEY must be the service_role key, not anon (this causes 500s)")
except Exception:
    print("Key role: could not decode (check SUPABASE_SERVICE_KEY)")
from supabase import create_client
db = create_client(url, key)
tables = ["users", "profiles", "preferences", "personality_profiles", "ai_twins", "likes", "matches", "conversations", "messages",
          "notifications", "video_dates", "reveal_requests", "photo_reveal_requests", "photo_reveal_permissions",
          "user_streaks", "verification_requests", "voice_prompts", "ideal_partner_prefs", "dating_requests",
          "breakup_feedback", "contests", "contest_entries", "contest_reveals"]
for t in tables:
    try:
        db.table(t).select("*").limit(1).execute(); print(f"  OK       {t}")
    except Exception as e:
        print(f"  MISSING/ERROR {t}: {str(e)[:150]}")
need = ["id", "first_name", "last_name", "email", "phone", "password_hash", "dob", "age", "gender", "interested_in",
        "onboarded", "is_verified", "created_at", "avatar", "verification_status"]
print("\nusers columns the signup needs:")
for c in need:
    try:
        db.table("users").select(c).limit(1).execute(); print(f"  OK       {c}")
    except Exception as e:
        print(f"  MISSING  {c}: {str(e)[:120]}")
print("\nprofiles columns:")
for c in ["user_id", "bio", "city", "occupation", "photos", "updated_at"]:
    try:
        db.table("profiles").select(c).limit(1).execute(); print(f"  OK       {c}")
    except Exception as e:
        print(f"  MISSING  {c}: {str(e)[:120]}")
import bcrypt; bcrypt.checkpw(b"x", bcrypt.hashpw(b"x", bcrypt.gensalt())); print("\nbcrypt works")
