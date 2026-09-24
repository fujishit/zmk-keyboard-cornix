# Keymap notes

Running log of keymap requests and decisions for `config/cornix.keymap`.
Keys are referred to by ZMK position number (row-major over `layout_50`).

## How to edit the keymap

`config/cornix.keymap` is the **source of truth** and is edited with
`scripts/remap.py`, not regenerated from the Vial export any more
(`scripts/vial2zmk.py` was the one-time importer; its command is recorded
below).  The CLI edits one layer's `bindings` block and the ASCII grid
comment above it in place, by key position, and runs the keymap checks from
`scripts/check_config.py` afterwards - a keymap that fails them is not
written.

```sh
python3 scripts/remap.py show                          # numbered grid, every layer
python3 scripts/remap.py show --layer Base             # one layer
python3 scripts/remap.py layer list
python3 scripts/remap.py set --layer Win 38 '&trans' 41 '&kp LCTRL'
python3 scripts/remap.py swap --layer Base 47 49
python3 scripts/remap.py copy --from-layer Base --to-layer Win 39 40
python3 scripts/remap.py clear --layer Win 38          # -> &trans
python3 scripts/remap.py layer add MouseFast           # a &trans-filled layer
python3 scripts/remap.py layer add Gaming --after Win  # ... and renumber &mo/&tog/&lt
python3 scripts/remap.py layer rename Fn4 Media
python3 scripts/remap.py --dry-run set --layer Base 0 '&kp ESC'   # just the diff
```

Layers are named (`Base`, `Win`, ...), addressed by display name, node name
(`layer_1`) or index.  Positions are `0`-`49`, bindings are full ZMK binding
strings (`'&kp MINUS'`, `'&mt LGUI SPACE'`, `'&trans'`); an unknown behavior
needs `--force`.  Exit status: 0 ok, 1 validation failure, 2 usage error.
`scripts/remap.py --help` has the rest.

Two things the CLI deliberately does not touch: `sensor-bindings` lines and
the header comment.  Hand-written nodes at the top of the keymap (the
`&mmv_input_listener` overlay that scales the pointer speed on the
MouseSlow/MouseFast layers, and the `app_tab` app-switch node in
`behaviors {}`) are likewise left alone.  The `&lt` override that used to tune the old
`&lt 6 RIGHT` mouse layer-tap was removed on 2026-09-19 together with that
layer-tap; the keymap now has no hold-tap at all.

The original import was:

```sh
python3 scripts/vial2zmk.py config/vial/cornix-default-keymap.vil \
    --layers 5 --layer-names Base,Num,Fn2,Fn3,Fn4 \
    --insert-layer 1:Win \
    --user-key 'USER00=&bt BT_SEL 0' \
    --user-key 'USER01=&bt BT_SEL 1' \
    --user-key 'USER02=&bt BT_SEL 2' \
    -o config/cornix.keymap
```

## Position map (Base layer labels, `remap.py show --layer Base`)

```
 0 TAB    1 Q     2 W     3 E     4 R     5 T    |                 |  6 Y     7 U     8 I     9 O    10 P    11 BSPC
12 CTRL  13 A    14 S    15 D    16 F    17 G    |                 | 18 H    19 J    20 K    21 L    22 \    23 ENTER
24 SHIFT 25 Z    26 X    27 C    28 V    29 B    | 30 MUTE 31 MCLK | 32 N    33 M    34 ,    35 .    36 UP   37 /
38 CTRL  39 CMD  40 OPT  41 CMD  42 Num  43 SPC  |                 | 44 SPC  45 Fn3  46 Fn2  47 LEFT 48 DOWN 49 RIGHT
```

Layers: 0 Base, 1 Win (OS-detection overlay), 2 Num (hold 42), 3 Fn2 (hold 46),
4 Fn3 (hold 45), 5 Conn (hold 45 + 46 together - a hand-written
`conditional_layers` node with `if-layers = <3 4>; then-layer = <5>;`),
6 MouseSlow (hold 45 + 2), 7 MouseFast (hold 45 + 3).  Layers 6 and 7 are
entirely `&trans`: they carry no bindings and exist only to switch the
`&mmv_input_listener` input processors (see the 2026-09-19 entry below).

Connection switching (2026-09-22) lives on Conn only: hold both right thumb
Fn keys, 45 and 46, then the leftmost column picks the connection - 0 = USB
(`&out OUT_USB`), 12 = Bluetooth (`&out OUT_BLE`), 24 = clear the bond
(`&bt BT_CLR`) - and the column next to it, 1 / 13 / 25, is BT profile
0 / 1 / 2 (`&bt BT_SEL n`) on the same rows.  Everything else on Conn is
`&trans`, so Space, Enter, Backspace, the arrows, the modifiers and the Fn3
mouse keys keep working while both thumbs are held.  Fn2 and Fn3 carry no
connection keys any more; only `&tog 1` (Fn3 + 0) stays on Fn3.

The mouse lives on Fn3: hold 45 and I/J/K/L (8/19/20/21) move the pointer
with the right hand, while the left hand does everything else - the three
left thumb keys 41/42/43 are left / right / middle click (44 is `&none`) and
W (2) / E (3) make the pointer slower (1/2) / faster (2x) while held.  D (15)
and F (16), which carried the speed keys between 2026-09-19 morning and
evening, are `&none` again.

Num (hold 42) also carries the symbols the Base layer has no room for:
`` ` `` on A (13), `[` / `]` on K/L (20/21) and `{` / `}` on `,` / `.`
(34/35), next to the existing `-` (17), `=` (18) and `'` (19).  Fn2 (hold 46)
carries F1-F12 across the whole top row (F12 on the Tab position 0, then
F1..F10 on 1..10 lining up with Num's N1..N0, F11 on 11) and the navigation
cluster on the right home row: HOME / PG_DN / PG_UP / END on H/J/K/L
(18/19/20/21, the same left-down-up-right shape as the mouse keys) plus
PSCRN (22) and INS (36).  The `&bt BT_SEL` column that used to sit on
12/24/38 moved to the Conn layer on 2026-09-22 (12 is `&none`, 24/38 fall
through to Shift/Ctrl).

The three left thumb keys are 41 (Cmd on macOS / Ctrl on Windows), 42 (Num)
and 43 (Space); 43 and 44 are both Space.  Fn3 sits on the right thumb,
position 45.

## Requests

| Date | Request (user's words, condensed) | Decision / status |
|---|---|---|
| 2026-09-17 | 伸ばし棒（ー = MINUS）がどの層でも出ない | **Done, no change needed**: MINUS is on Num + position 17 (`&mo 2` on 42 + G), confirmed working. |
| 2026-09-17 | 入力切替は Mac の US 配列と同じく Ctrl+Space にしたい | **Done**: the physical Ctrl key (38) stays LCTRL on both operating systems, so Ctrl+Space toggles the IME everywhere; only the shortcut modifier moves on the Win layer. `python3 scripts/remap.py set --layer Win 38 '&trans' 41 '&kp LCTRL'` (39 was already `&kp LCTRL`, 40 stays `&trans` = LALT). |
| 2026-09-17 | 左親指の常駐キーを Mac では Cmd、Windows では Ctrl に | **Done**: position 41 is the rest key (`&kp LGUI` on Base, `&kp LCTRL` on Win) and the layer keys moved one key inwards, dropping the duplicate Space. `python3 scripts/remap.py set --layer Base 41 '&kp LGUI' 42 '&mo 2' 43 '&mo 4'` and `... set --layer Win 41 '&kp LCTRL'`. Num is now held with 42 and Fn3 with 43. |
| 2026-09-17 | Fn3+T / Fn3+Y の `&bootloader` は不要（リセット2回押しで運用） | **Done**: `python3 scripts/remap.py set --layer Fn3 5 '&none' 6 '&none'` (the Vial default had KC_NO there). The halves are flashed with a reset double-tap; `scripts/FLASHING.md` still describes the removed key and needs an update. |
| 2026-09-18 | 41（親指の Cmd/Ctrl）+ Tab を Mac では Cmd+Tab、Windows では Alt+Tab に | **Superseded on 2026-09-19** (see the last row): the mod-morph was replaced by the `app_tab` app-switch behavior, because its Alt could not stay held.  Was: Win レイヤの position 0 を `&win_tab` に (`python3 scripts/remap.py set --layer Win 0 '&win_tab'`).  `win_tab` is a `zmk,behavior-mod-morph` defined in the keymap's `behaviors` node: `bindings = <&kp TAB>, <&kp LA(TAB)>`, `mods = <(MOD_LCTL|MOD_RCTL)>`, no `keep-mods`, so the triggering Ctrl is masked out of the report and the host sees a clean Alt+Tab.  macOS needs nothing: 41 is LGUI on Base, so the same chord is Cmd+Tab.  **This is the one deliberate mod-morph**: the preference is plain per-layer key changes, and a mod-morph only where the same physical chord must come out with a different modifier per OS.  Accepted caveat: this is not macOS's Cmd+Tab switcher - tapping Tab again while holding 41 re-sends Alt+Tab (Windows flips between the two most recent windows), while holding Tab keeps the switcher open.  Covered by `tests/sim/user-keymap/os-detect-windows` (LALT+TAB, no LCTRL in the report) and `os-detect-macos` (LGUI+TAB). |
| 2026-09-18 | 43 を Space に戻し、Fn3 は右親指（45）に移したい | **Done**: `python3 scripts/remap.py set --layer Base 43 '&kp SPACE' 45 '&mo 4'`.  The left thumb is now 41 Cmd/Ctrl, 42 Num, 43 Space, and 43/44 are both Space again; Fn3 is held with 45, which used to be `&mo 5` (Fn4).  The Fn4 layer (5) is left in the keymap but is unreachable - it was entirely `&trans` and nothing else references it, so removing it would only renumber Mouse (6) and every `&mo`/`&lt` that points at it.  Connectivity keys are now "hold 45 + 38" for `&out OUT_BLE` and "hold 45 + 0" for the manual `&tog 1`.  `tests/sim/user-keymap/os-detect-windows` holds RC(3,11) (= position 45) instead of RC(3,5). |
| 2026-09-17 | マウス操作を追加したい | **Superseded on 2026-09-19** (see the last row). Was: new layer 6 `Mouse`, held with `&lt 6 RIGHT` on position 49 (tap = RIGHT). `python3 scripts/remap.py layer add Mouse`, then `python3 scripts/remap.py set --layer Mouse 8 '&mmv MOVE_UP' 19 '&mmv MOVE_LEFT' 20 '&mmv MOVE_DOWN' 21 '&mmv MOVE_RIGHT' 42 '&mkp LCLK' 43 '&mkp RCLK'` and `python3 scripts/remap.py set --layer Base 49 '&lt 6 RIGHT'`. The `&lt` override node (`flavor = "balanced"`, `tapping-term-ms = <200>`, `quick-tap-ms = <200>`) was added by hand; `quick-tap-ms` is what makes tap-then-hold repeat RIGHT instead of switching layers. Covered by `tests/sim/user-keymap/mouse-layer`. |
| 2026-09-18 | Fn3+12（CAPS 位置）の `&bt BT_CLR` を誤爆して Mac のボンドが消えた。押しにくい位置へ | `remap.py set --layer Fn3 12 '&none' 11 '&bt BT_CLR'`: BT_CLR is now Fn3 (hold 45) + 11 (top-right corner, BSPC position), away from the `&out` keys on the left column. Done. |
| 2026-09-19 | マウス操作は Fn3 レイヤ（45 押しっぱなし）に。IJKL で WASD 風に移動（8 上 / 19 左 / 20 下 / 21 右）、45+43 左クリック、45+44 右クリック、7 (U) / 9 (O) は押している間だけカーソル速度を遅く／速く。49 は `&kp RIGHT` に戻す | **Done**. The mouse moved off its own layer-tap layer and onto Fn3, so there is no hold-tap in the keymap any more and the `&lt` override node was deleted by hand (documented in the keymap header). Two new `&trans`-only layers carry the speed: the old, now unused `Mouse` layer 6 was renamed and emptied to become `MouseSlow`, and `MouseFast` (7) was appended, so only one layer had to be added and every `&mo` index stayed put. The layers hold no bindings - they are flags for the hand-written `&mmv_input_listener` overlay (`slow { layers = <6>; input-processors = <&zip_xy_scaler 1 3>; }`, `fast { layers = <7>; ... <&zip_xy_scaler 3 1>; }`, plus `#include <input/processors.dtsi>`). `zmk/app/src/pointing/input_listener.c`, `filter_with_input_config()`, selects an override purely with `zmk_keymap_layer_active()`, never by the layer the `&mmv` binding resolved on, so the scaling applies even though the `&mmv` keys live on Fn3 underneath. Commands: `python3 scripts/remap.py layer rename Mouse MouseSlow`, `python3 scripts/remap.py clear --layer MouseSlow 8 19 20 21 42 43`, `python3 scripts/remap.py layer add MouseFast`, `python3 scripts/remap.py set --layer Fn3 8 '&mmv MOVE_UP' 19 '&mmv MOVE_LEFT' 20 '&mmv MOVE_DOWN' 21 '&mmv MOVE_RIGHT' 43 '&mkp LCLK' 44 '&mkp RCLK' 7 '&mo 6' 9 '&mo 7'`, `python3 scripts/remap.py set --layer Base 49 '&kp RIGHT'`. Covered by `tests/sim/user-keymap/mouse-layer` (rewritten). |
| 2026-09-19 | 41（親指の Cmd/Ctrl）+ Tab は Windows でも Mac のように、Alt を押しっぱなしのままスイッチャーを開いたまま Tab を送りたい | **Done**: `win_tab` の mod-morph を捨て、このモジュール独自のビヘイビア `zmk,behavior-cornix-app-switch` (`src/behavior_app_switch.c`, `dts/bindings/behaviors/zmk,behavior-cornix-app-switch.yaml`, `CONFIG_CORNIX_APP_SWITCH` は既定 y) に置き換えた。keymap の `behaviors {}` に `app_tab` ノードを手書きし (`hold-mod = <LCTRL>`, `switch-mod = <LALT>`, `tap = <TAB>`, `mod-position = <41>`)、`python3 scripts/remap.py set --layer Win 0 '&app_tab'`。**なぜ mod-morph では駄目だったか**: `&kp LA(TAB)` の LALT は keypress の *implicit* modifier で、ZMK は implicit modifier をキーと一緒に離す (`zmk/app/src/hid_listener.c` の `zmk_hid_implicit_modifiers_release()`, `hid_listener_keycode_released()` 内)。つまり Tab を離すたびに Alt も離れ、Windows のスイッチャーはその場で確定して閉じてしまう。新しいビヘイビアは 41 が Ctrl を押している間だけ、その Ctrl を `zmk_hid_masked_modifiers_set()` でレポートから隠し、LALT を *本物の* modifier として押し、TAB を 1 回タップする。41 を押したままの 2 回目以降の押下は TAB のタップだけを繰り返すので Alt は下がったまま = スイッチャーは開いたまま次の窓へ進む。41 を離すと（`zmk_position_state_changed` を購読して検出）LALT を離してマスクを解除する — これが Windows 側の「確定」。Ctrl は押し直さない（ユーザーが既に離しているため）。Ctrl を押していない状態で押せば素の `&kp TAB`（押しっぱなしのリピートも効く）。macOS 側は従来どおり何も要らない（41 は Base で LGUI、position 0 は素の `&kp TAB`）。制限として受け入れたもの: Alt モードは **41 を離すまで** 続くので、Alt+Tab のあと 41 を押したままほかのキーを押すとそのキーにも Ctrl ではなく Alt が乗る。カバーするテスト: `tests/sim/user-keymap/app-switch`（専用ケース。Tab 2 回で LALT 0xE2 の press が 1 回だけ、41 のリリースで 0xE2 release、その後の素の TAB まで）と `tests/sim/user-keymap/os-detect-windows`、macOS 側は `os-detect-macos`（LGUI+TAB のまま変更なし）。 |
| 2026-09-19 | BLE ペアリングのパスキーを打つと、その数字が USB 側のホストにも入力されてしまう | **Not fixable from this repository; documented instead** (`scripts/log/README.md` の 5c)。ZMK のパスキー収集リスナ (`zmk/app/src/ble.c`, `zmk_ble_handle_key_user()`) は数字を `ZMK_EV_EVENT_HANDLED` で握り潰しているが、イベントマネージャはリンク順に `.event_subscription` を走査する (`zmk/app/src/event_manager.c:20-46`, `zmk/app/include/linker/zmk-events.ld` は `SORT` なしの `KEEP`) ため、`zmk/app/CMakeLists.txt` で先に並ぶ `src/hid_listener.c`(76 行目) が `src/ble.c`(86 行目) より先に走り、握り潰される前にレポートを送ってしまう。モジュールのライブラリはアプリのオブジェクトより後にリンクされるので、こちらのリスナを前に置くことはできない（デバッグビルドの `zmk.map` で確認: `zmk_event_sub_hid_listener...` < `zmk_event_sub_zmk_ble...` < `zmk_event_sub_cornix_...`）。パスキー中かどうかを外から知る手段もない（`auth_passkey_entry_conn` は `ble.c` の static、`bt_conn_auth_cb` の登録はグローバルに 1 つだけで ZMK が保持済み）。**回避策**: パスキーは Num レイヤ（42 押しっぱなし）で打ち、その間 USB ホスト側は無害な窓（スクラッチのテキスト欄やデスクトップ）にフォーカスしておく。ESC はキャンセル、RETURN は確定で、これらも同じく漏れる。 |
| 2026-09-19 | マウス速度キー（U/O）は右手だと押しにくいので左手へ | `remap.py set --layer Fn3 7 '&none' 9 '&none' 15 '&mo 6' 16 '&mo 7'`: hold 45, then D (15) = slow 1/3, F (16) = fast 3x (F for fast). Sim case mouse-layer updated. Done. |
| 2026-09-19 | クリックは両方左親指に: 45+42 左クリック、45+43 右クリック | `remap.py set --layer Fn3 42 '&mkp LCLK' 43 '&mkp RCLK' 44 '&none'`. Done. |
| 2026-09-19 | 右のロータリーエンコーダのスクロールが鈍い（1 ノッチ 1 行で、速く回すと追いつかない） | **Done**, `config/cornix.keymap` を手編集（`behaviors {}` と include 部はどちらも `remap.py` の対象外）。2 箇所だけ: (1) `#include <dt-bindings/zmk/pointing.h>` の**直前**に `#define ZMK_POINTING_DEFAULT_SCRL_VAL 188`、(2) `inc_dec_msc` の `tap-ms` を `150` → `24`。**計算**: `&msc` (`zmk/app/dts/behaviors/mouse_scroll.dtsi:12`) は `zmk,behavior-input-two-axis` で、押すと *速度* が立ち、`trigger-period-ms`（既定 16、`zmk,behavior-input-two-axis.yaml:17-20`）ごとに `speed * 16 / 1000` を小数アキュムレータに足して整数部だけを報告する（`behavior_input_two_axis.c:131-155`）。`&msc` は `acceleration-exponent = <0>` なので `speed()` は最初の tick から満速を返し、`time-to-max-speed-ms = <300>` は効かない（同 `:111-129`）。よって 1 tick = `SCRL_VAL * 16 / 1000` 単位。3 単位が欲しければ `3 * 1000 / 16 = 187.5` → **188**（`188 * 16 / 1000 = 3.008` → 3 を報告、余り 0.008）。一方 `tap-ms` は press と release の間隔であると同時に、ビヘイビアキューが直列なのでノッチ 1 個あたりの**所要時間**そのもの（`behavior_sensor_rotate_common.c:98-101` → `behavior_queue.c:53-55`）。16 ms の tick を 1 回だけ挟めばよいので `16 < tap-ms < 32`、中央の **24 ms**（前後 8 ms の余裕）。従来は `SCRL_VAL=10` × `tap-ms=150` で 9 tick × 0.16 = 1.44 → 1 単位/ノッチ・150 ms/ノッチだった。**シム実測** (`tests/sim/user-keymap/encoder`、右エンコーダを 20 ms 間隔で 3 ノッチずつ両方向に回す): 変更前は最初のスクロールが入力から 118 ms 後、以後 151 ms 間隔、最後のノッチから 773 ms 遅れて終了（各 1 単位）。変更後は 16 ms 後、以後 25 ms 間隔、最後のノッチから 41 ms（各 3 単位）= レートで約 19 倍、レイテンシで約 1/7。左（音量）エンコーダの出力はスナップショット上で完全に不変。**注意**: 1 wheel unit はホスト側では「1 ノッチ」であって 1 行ではない。ブラウザは 1 ノッチ ≒ 3 行なので体感は約 9 行/ノッチ、macOS はさらに慣性がつく。強すぎ／弱すぎたら `ZMK_POINTING_DEFAULT_SCRL_VAL` だけを触る（1 単位あたり 63）。`SCRL_*` を使っているのはこのエンコーダだけで、Fn3 のマウス移動は `MOVE_*`（別定数 `ZMK_POINTING_DEFAULT_MOVE_VAL`）なので影響なし。方向は `<&inc_dec_msc SCRL_DOWN SCRL_UP>` のまま（逆なら 2 引数を入れ替える）。 |
| 2026-09-19 | クリックは 3 つとも左親指に: 45+41 左、45+42 右、45+43 中クリック | **Done**: `python3 scripts/remap.py set --layer Fn3 41 '&mkp LCLK' 42 '&mkp RCLK' 43 '&mkp MCLK'`.  44（右親指の Space）は `&none` のまま、30/31（中央の MUTE / MCLK）も変更なし。左親指 3 つが左・右・中クリックになったので、右手は I/J/K/L のカーソル移動に専念できる。`tests/sim/user-keymap/mouse-layer` のケース 2-4 が 41=RC(3,3) / 42=RC(3,4) / 43=RC(3,5) を押して `Button 0` / `Button 1` / `Button 2`（`Mouse buttons set to 0x01` / `0x02` / `0x04`）を確認する。 |
| 2026-09-19 | マウス速度キーは D/F だと移動キーと干渉するので W/E へ。倍率も 1/3・3 倍はきつすぎるので 1/2・2 倍に | **Done**: `python3 scripts/remap.py set --layer Fn3 2 '&mo 6' 3 '&mo 7' 15 '&none' 16 '&none'` でキー位置を移し、`&mmv_input_listener` の `slow` / `fast` を手編集して `&zip_xy_scaler 1 3` → `1 2`、`3 1` → `2 1` に（この overlay は `remap.py` の対象外。keymap ヘッダのコメントも同時に更新）。**シム実測** (`tests/sim/user-keymap/mouse-layer`、どの走行も position 8 を 200 ms 押す): 素の速度が 1+2+2+3+3+4+4+5+5+6 = **35 単位 / 10 レポート**、MouseFast (2/1) が 2+4+4+6+6+8+8+10+10+12 = **70 単位** = ちょうど 2 倍、MouseSlow (1/2) が 1+1+2+1+2+2+3+2+3 = **17 単位 / 9 レポート** ≒ 35/2（1 レポートが 0 に丸まって `events.patterns` に落とされる）。`layer_id: 7 position: 8, binding name: transparent` → `layer_id: 4 ... mouse_move` の解決順もスナップショットに残っている。 |
| 2026-09-19 | プログラミング用の記号がどの層にも無い（バッククォート・角括弧・波括弧） | **Done**, Num レイヤ（42 押しっぱなし）に**追加のみ**: `python3 scripts/remap.py set --layer Num 13 '&kp GRAVE' 20 '&kp LBKT' 21 '&kp RBKT' 34 '&kp LBRC' 35 '&kp RBRC'`。既存の `-`(17) `=`(18) `'`(19) と同じ行・同じ側に並ぶので、`[` `]` が K/L、`{` `}` がその真下の `,` `.`。`LBRC`/`RBRC` は ZMK の `dt-bindings/zmk/keys.h` では `LS(LBKT)`/`LS(RBKT)` なので、HID 上は `[`/`]` と同じ usage (`0x2F`/`0x30`) に implicit modifier `0x02` (LEFT_SHIFT) が乗る形で出る（波括弧専用の usage は存在しない）。カバーするテスト: 新設の `tests/sim/user-keymap/symbols`。 |
| 2026-09-19 | ファンクションキーと Home/End/PageUp/PageDown が無い | **Done**, Fn2 レイヤ（46 押しっぱなし）に**追加のみ**（12/24/38 の `&bt BT_SEL` と 30/31 はそのまま）: `python3 scripts/remap.py set --layer Fn2 0 '&kp F12' 1 '&kp F1' 2 '&kp F2' 3 '&kp F3' 4 '&kp F4' 5 '&kp F5' 6 '&kp F6' 7 '&kp F7' 8 '&kp F8' 9 '&kp F9' 10 '&kp F10' 11 '&kp F11' 18 '&kp HOME' 19 '&kp PG_DN' 20 '&kp PG_UP' 21 '&kp END' 22 '&kp PSCRN' 23 '&kp INS'`。F12 を Tab 位置（0）に置いたのは、1..10 が Num レイヤの N1..N0 とそのまま重なるようにするため（F11 は 11 = BSPC 位置）。ナビゲーションは H/J/K/L に HOME/PG_DN/PG_UP/END で、矢印キーや Fn3 のマウスキーと同じ「左・下・上・右」の形。カバーするテスト: 新設の `tests/sim/user-keymap/fn2-keys`。 |
| 2026-09-19 | `scripts/vial2zmk.py` が古いエンコーダ設定を吐いたままになっている | **Done**: 一度きりの取り込みスクリプトとはいえ、再実行したときに 2026-09-19 のスクロール調整が巻き戻るので、テンプレートを現行の `config/cornix.keymap` に合わせた。`SCROLL_BEHAVIOR` の `tap-ms = <150>` を `<24>` にし、新しい `SCROLL_DEFINE`（`#define ZMK_POINTING_DEFAULT_SCRL_VAL 188` + 短い由来コメント、詳しい導出はこのファイルの上のエンコーダの行を参照）を `#include <dt-bindings/zmk/pointing.h>` の**直前**に出力するようにした（pointing.h が `#ifndef` 越しに SCRL_* を導出するため順序が効く）。`SCRL_VAL = 10` を前提にした古いコメントも書き換え。`tests/vial/test_vial2zmk.py` に define / `tap-ms = <24>` / 両者の出力順を確認する assert を追加。 |
| 2026-09-21 | Windows キーが無かった（Win 層で 39 も 41 も Ctrl になっていた） | `remap.py set --layer Win 39 '&trans'`: on Windows 39 stays LGUI = Windows key, 41 = Ctrl (thumb rest), 38 = Ctrl. On macOS 39 and 41 are both Cmd. Done. |
| 2026-09-21 | レイヤーキーを先に押すと Shift/Ctrl が効かない（Num/Fn 層の修飾キー位置が `&none` だった） | Num: 12,24,38,39,40,41 → `&trans`; Fn2: 39,40,41 → `&trans`; Fn3: 39,40 → `&trans`. Modifiers now work regardless of press order (Fn2 24/38 = BT1/BT2 and Fn3 24/38 = USB/BLE keep their bindings). Done. |
| 2026-09-22 | Backspace（11）はどの層でも効くようにしたい | 11 → `&trans` on Num/Fn2/Fn3. Moved: DEL → Num 23 (Enter pos), F11 → Fn2 23, INS → Fn2 36 (UP pos), BT_CLR → Fn3 23 (Enter pos). Done. |
| 2026-09-22 | Space / Enter / 矢印もレイヤーによらず効くように | Num/Fn2: 43,44,47,48,49,23 → `&trans`; Fn3: 44,47,48,49,23 → `&trans` (43 stays MCLK). Moved: DEL → Num 12 (CAPS pos), F11 → Fn2 13 (A), BT_CLR → Fn3 37 (`/` pos, bottom-right corner). Always-through keys now: 11 Backspace, 23 Enter, 43/44 Space (43 = middle click on Fn3), 47-49 arrows, modifiers 24/38-41 (except Fn2 24/38 BT1/BT2, Fn3 24/38 USB/BLE, Fn3 41/42 clicks). Done. |
| 2026-09-22 | USB/BLE 切替の位置が覚えられない | Fn3: 7 (U) = `&out OUT_USB`, 29 (B) = `&out OUT_BLE`; 24/38 → `&trans` (Shift/Ctrl pass through on Fn3 now). Mnemonic: 45+U = USB, 45+B = Bluetooth. Done. |
| 2026-09-22 | USB/BLE 切替は Fn3 だけの単キーコードだと誤爆する（45 はマウスで常に押す）し、やはり場所が覚えられない。45 と 46 を両方押している間だけ出る層にして、左端の列で選びたい | **Done**: layer 5 (`Fn4`, empty) renamed to `Conn` and made a **conditional layer** - hand-written `conditional_layers { conn_layer { if-layers = <3 4>; then-layer = <5>; }; }` next to `keymap` (Fn2 = layer 3 = position 46, Fn3 = layer 4 = position 45; `remap.py` leaves the node alone). Mnemonic: **hold both thumb Fn keys 45 + 46, then the leftmost column: top = USB, 2nd = Bluetooth, 3rd = clear bond; the column next to it = profile 0 / 1 / 2.** Commands: `python3 scripts/remap.py layer rename Fn4 Conn`, `python3 scripts/remap.py clear --layer Conn 0 ... 49`, `python3 scripts/remap.py set --layer Conn 0 '&out OUT_USB' 12 '&out OUT_BLE' 24 '&bt BT_CLR' 1 '&bt BT_SEL 0' 13 '&bt BT_SEL 1' 25 '&bt BT_SEL 2'`; every other Conn key is `&trans` (not `&none`), so Space / Enter / Backspace / arrows / modifiers and the Fn3 mouse keys still fall through while both thumbs are held. Removed from the other layers: `python3 scripts/remap.py set --layer Fn3 7 '&none' 29 '&none' 37 '&none'` (the 45+U / 45+B / 45+`/` chords) and `python3 scripts/remap.py set --layer Fn2 12 '&none' 24 '&trans' 38 '&trans'` (BT_SEL 0/1/2; 24/38 are Shift/Ctrl on Base and modifiers must work on every layer). **Found by the sim**: 46 on Fn3 and 45 on Fn2 were `&none`, so with one Fn layer up the other thumb key hit a dead key and the second layer never came on; `python3 scripts/remap.py set --layer Fn3 46 '&trans'` and `... set --layer Fn2 45 '&trans'` make them fall through to their `&mo` on Base, so the chord works in either order. Covered by `tests/sim/user-keymap/conn-layer` (Conn activates on 45 + 46, `&out` resolves on layer 5, Backspace/Space fall through, Conn deactivates when 46 is released) and `tests/remap` / `tests/cheatsheet`. |
| 2026-09-24 | BT_CLR が怖いので消す | Conn: 24 `&bt BT_CLR` → `&trans`. ボンド消去は settings_reset image か一時的に keymap へ戻して行う。`remap.py set --layer Conn 24 '&trans'` |
| 2026-09-24 | Mac で 12（Caps の位置）を Ctrl として使いたい | Base 12 `&kp CAPS` → `&kp LCTRL`（両 OS）、Caps Lock は Fn2 (46) + 12 へ。`remap.py set --layer Base 12 '&kp LCTRL'` / `set --layer Fn2 12 '&kp CAPS'` |
