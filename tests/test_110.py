import io, os, stat, sys, contextlib, tempfile, warnings; warnings.filterwarnings("ignore")
D = tempfile.mkdtemp(); os.environ["DATA_DIR"] = D; os.environ.pop("ADMIN_PASSWORD", None)
os.environ["ADMIN_USER"] = "boss"; os.environ.pop("ADMIN_RESET_TOKEN", None)
sys.path.insert(0, sys.argv[1])
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    import auth
    auth.init_admin()
log = buf.getvalue(); f = auth.INITIAL_PW_FILE
pw = open(f).read().split("password: ")[1].strip()
ok = True
def check(n, c):
    global ok; ok &= bool(c); print(("✓ " if c else "✗ ") + n)
print("лог:", log.strip())
check("пароля нет в логе", pw not in log)
check(f"файл создан, права {oct(stat.S_IMODE(os.stat(f).st_mode))}", stat.S_IMODE(os.stat(f).st_mode) == 0o600)
check(f"с паролем из файла можно войти (длина {len(pw)})", auth.verify("boss", pw))
auth.create_user("ivan", "ivanpass1"); auth.set_password("ivan", "ivanpass2")
check("смена пароля ДРУГОГО пользователя файл не трогает", os.path.exists(f))
auth.set_password("boss", "newbosspass")
check("после смены пароля админом файл удалён", not os.path.exists(f))
