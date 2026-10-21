import io, os, sys, contextlib, tempfile, warnings; warnings.filterwarnings("ignore")
D = tempfile.mkdtemp(); os.environ.update(DATA_DIR=D, ADMIN_USER="boss", ADMIN_PASSWORD="SecretEnvPw1")
sys.path.insert(0, sys.argv[1]); buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    import auth; auth.init_admin()
print("лог:", buf.getvalue().strip())
ok = "SecretEnvPw1" not in buf.getvalue() and not os.path.exists(auth.INITIAL_PW_FILE) and auth.verify("boss", "SecretEnvPw1")
print(("✓ " if ok else "✗ ") + "с ADMIN_PASSWORD: пароля в логе нет, файл не создан, вход работает")
