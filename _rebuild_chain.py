# -*- coding: utf-8 -*-
"""还原变异、重建整条补丁链（7 个包）。"""
import os, shutil, json, hashlib, subprocess

ROOT = r'D:\签到系统'
os.chdir(ROOT)
PY = os.path.join('runtime', 'python.exe')

# ① 还原被变异的引擎
mut = os.path.join('不在区域内重跑补丁包', 'patch_engine.py.mutbak')
if os.path.exists(mut):
    open(os.path.join('不在区域内重跑补丁包', 'patch_engine.py'), 'wb').write(open(mut, 'rb').read())
    os.remove(mut)
    print('[还原] 不在区域内重跑补丁包/patch_engine.py')

# ② 恢复到 ed1f161e（P1+P2 已装的状态）
shutil.copy(os.path.join('僵尸窗口过滤补丁包', '_backup', 'signin.py.base'), 'signin.py')
sha0 = hashlib.sha256(open('signin.py', 'rb').read()).hexdigest()
print('[基线] signin.py =', sha0[:16], '(应 ed1f161e500fc2a7)')
assert sha0.startswith('ed1f161e500fc2a7'), '基线不对'

# ③ 清掉后 5 个包的状态与注册表条目（Z/P/E/N/D）
NEW5 = ['僵尸窗口过滤补丁包', '进程检测补丁包', '进入微信判定补丁包',
        '不在区域内重跑补丁包', '记录日期判定补丁包']
for pkg in NEW5:
    p = os.path.join(pkg, '_state.json')
    if os.path.exists(p):
        os.remove(p)
reg = '.patch_packages.json'
d = json.load(open(reg, encoding='utf-8'))
for k in NEW5:
    d['packages'].pop(k, None)
json.dump(d, open(reg, 'w', encoding='utf-8'), ensure_ascii=False, indent=2)
print('[清理] 注册表剩余:', list(d['packages'].keys()))

# ④ 按顺序重装
ORDER = ['僵尸窗口过滤补丁包', '进程检测补丁包', '进入微信判定补丁包',
         '不在区域内重跑补丁包', '记录日期判定补丁包']
print()
for pkg in ORDER:
    r = subprocess.run([PY, os.path.join(pkg, 'patch_engine.py'), 'apply'],
                       capture_output=True, text=True, encoding='utf-8', errors='replace')
    out = ((r.stdout or '') + (r.stderr or '')).strip().splitlines()
    ok = '安装失败' not in '\n'.join(out)
    sha = hashlib.sha256(open('signin.py', 'rb').read()).hexdigest()[:16]
    print('  %-22s %s  -> %s' % (pkg, '✅' if ok else '❌', sha))
    if not ok:
        for ln in out:
            print('       ' + ln)

print()
print('[最终] signin.py =', hashlib.sha256(open('signin.py', 'rb').read()).hexdigest())
