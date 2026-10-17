"""從 Phn.tbl 依縮點規則產生縮點表，並可和高點的 chi.key 比對。

縮點規則（構想：hurt；高點實作：高生旺）：
  聲調用第 7、8 點與空白鍵加在最後一方：一聲加空白、二聲加點7、三聲加點7+8、四聲加點8、輕聲不加
  單方字（只有一方的音節）輕聲加點8 再加空白
  進階：玻坡摸佛（ㄛ）、的特呢了哥科喝（ㄜ）、之吃失日資疵司（空韻）省略韻母，聲調加在聲母那一方；
        輕聲一樣加點8 再加空白
用法：
  python build_suodian.py                 產生 globalPlugins/sixdIME/suodian.tbl
  python build_suodian.py --compare chi.key   和 chi.key 比對
"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLUGIN = os.path.join(HERE, '..', 'globalPlugins', 'sixdIME')
NABCC = " a1b'k2l@cif/msp\"e3h9o6r\\djg>ntq,*5<-u8v.%[$+x!&;:4|0z7(_?w]#y)="
TONE_KEY = {1: ' ', 2: '6', 3: '3', 4: '4', 5: '7'}
DOT7, DOT8 = 0x40, 0x80
# 進階打法：大千鍵位（聲母＋ㄛ／ㄜ，或只有聲母的空韻）
ADVANCED = {'1i', 'qi', 'ai', 'zi',                       # 玻坡摸佛
            '2k', 'wk', 'sk', 'xk', 'ek', 'dk', 'ck',     # 的特呢了哥科喝
            '5', 't', 'g', 'b', 'y', 'h', 'n'}            # 之吃失日資疵司


def load_phn(path):
    raw = open(path, 'rb').read().decode('utf-8', errors='replace')
    raw = raw.replace('\x1a', '').replace('\r\n', '\n').replace('\r', '\n')
    lines = [l for l in raw.split('\n') if l]
    out = []
    for i in range(0, len(lines) - 2, 3):
        brl, key, mask = lines[i], lines[i + 1], lines[i + 2]
        if brl and all(c in NABCC for c in brl):
            out.append(([NABCC.index(c) for c in brl], key, mask))
    return out


def with_tone(cell, tone):
    """回傳 (點, 有沒有空白)。"""
    return {1: (cell, True), 2: (cell | DOT7, False), 3: (cell | DOT7 | DOT8, False),
            4: (cell | DOT8, False), 5: (cell, False)}[tone]


def build(phn):
    """{((點, 空白), ...): 大千鍵位含調號}"""
    table = {}
    for cells, key, mask in phn:
        for tone in range(1, 6):
            last = with_tone(cells[-1], tone)
            if tone == 5 and len(cells) == 1:
                # 單方字的輕聲加點8 再加空白：只打一方會和標點的第一方撞在一起（例：345＋空白＝〉）
                last = (cells[0] | DOT8, True)
            seq = tuple((c, False) for c in cells[:-1]) + (last,)
            table.setdefault(seq, key + TONE_KEY[tone])
            if key in ADVANCED:
                init = cells[0]
                adv = (init | DOT8, True) if tone == 5 else with_tone(init, tone)
                table.setdefault((adv,), key + TONE_KEY[tone])
    return table


def fmt(seq):
    def cell(c):
        bits, space = c
        s = ''.join(str(i + 1) for i in range(8) if bits & (1 << i))
        return ('0' if space else '') + s
    return '-'.join(cell(c) for c in seq)


def parse_key(s):
    seq = []
    for part in s.split('-'):
        bits = 0
        for ch in part.replace('0', ''):
            bits |= 1 << (int(ch) - 1)
        seq.append((bits, '0' in part))
    return tuple(seq)


def main():
    phn = load_phn(os.path.join(PLUGIN, 'Phn.tbl'))
    table = build(phn)
    if '--compare' in sys.argv:
        sys.stdout.reconfigure(encoding='utf-8')
        path = sys.argv[sys.argv.index('--compare') + 1]
        chi = {}
        for line in open(path, encoding='utf-8-sig'):
            if '\t' in line:
                k, v = line.rstrip('\r\n').split('\t', 1)
                if k != '0':
                    chi[parse_key(k)] = v
        same = [k for k in chi if table.get(k) == chi[k]]
        diff = [k for k in chi if k in table and table[k] != chi[k]]
        only_chi = [k for k in chi if k not in table]
        only_ours = [k for k in table if k not in chi]
        print(f'chi.key {len(chi)} 筆，產生 {len(table)} 筆；相同 {len(same)}，鍵位不同 {len(diff)}，'
              f'只在 chi.key {len(only_chi)}，只在產生的表 {len(only_ours)}')
        for title, ks, f in (('鍵位不同', diff, lambda k: f'{fmt(k)}\tchi={chi[k]!r}\t產生={table[k]!r}'),
                             ('只在 chi.key', only_chi, lambda k: f'{fmt(k)}\t{chi[k]!r}'),
                             ('只在產生的表', only_ours, lambda k: f'{fmt(k)}\t{table[k]!r}')):
            print(f'\n== {title}（{len(ks)}）')
            for k in sorted(ks, key=fmt):
                print(f(k))
        return
    out = os.path.join(PLUGIN, 'suodian.tbl')
    with open(out, 'w', encoding='utf-8', newline='\n') as fh:
        fh.write('# 縮點表：由 tools/build_suodian.py 從 Phn.tbl 產生，不要手動修改\n')
        fh.write('# 點位（0＝空白，- 分隔每一方）\t大千鍵位（含調號）\n')
        for seq in sorted(table, key=fmt):
            fh.write(f'{fmt(seq)}\t{table[seq]}\n')
    print(f'寫入 {out}：{len(table)} 筆')


if __name__ == '__main__':
    main()
