import sys, time, threading, httpx
url = sys.argv[1]
def bad(idx):
    for i in range(4):
        httpx.post(url + "/api/login", json={"username": f"u{idx}_{i}", "password": "x"}, timeout=60)
ths = [threading.Thread(target=bad, args=(n,)) for n in range(10)]
[t.start() for t in ths]
time.sleep(0.2)
lat = []
while any(t.is_alive() for t in ths):
    t0 = time.time(); httpx.get(url + "/health", timeout=60); lat.append(time.time() - t0); time.sleep(0.05)
print(f"/health во время 40 входов: макс {max(lat)*1000:.0f} мс, медиана {sorted(lat)[len(lat)//2]*1000:.0f} мс")
