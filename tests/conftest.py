"""The tests assume the defaults, whatever the developer's .env turns on (a variable already set wins over .env)."""
import os

os.environ["MASTERSMITH_LLM"] = "openrouter"
os.environ["MASTERSMITH_NO_SPEND"] = "0"
os.environ["MASTERSMITH_LOCAL_TRELLIS_TEX_RES"] = ""
os.environ["MASTERSMITH_PAID_PICTURES"] = "0"
os.environ["MASTERSMITH_BODY_SEED_VIEW"] = ""
