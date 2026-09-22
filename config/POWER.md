# POWER — 充電・電池・省電力の調査メモ

対象: `cornix_left` / `cornix_right`（nRF52840 / E73-2G4M08S1C）、両半分とも 1S LiPo +
物理電源スイッチ、USB-C 充電。

実体を確認したもの: `boards/jzf/cornix/*`（`Kconfig`, `Kconfig.defconfig`, `pinmux.c`,
`nrf_e73.dtsi`, `cornix.dtsi`, `cornix_sensors.dtsi`, `*_defconfig`）、`config/cornix_left.conf`、
`boards/shields/cornix_indicator/*`、ビルド済み `.config`
（`.build/led/cornix_left_full_p256m_debug/`、`.build/keymap/cornix_right_tx8_log/`）、
ZMK 本体 `.sim/ws/zmk/app/src/{battery,activity,pm}.c` と `app/Kconfig`、ZMK docs
（`features/low-power-states.md`, `config/power.md`）、RMK 参考 config。

**このメモは調査結果のみ。`*.conf` / `*.dts*` は一切変更していない。**

---

## 0. 現状 / 推奨 / 効果 / 副作用

| 項目 | 現状 | 推奨 | 効果 | 副作用 |
| --- | --- | --- | --- | --- |
| `CONFIG_ZMK_SLEEP`（ディープスリープ） | 両半分とも **n** | 右のみ `y` + `CONFIG_ZMK_IDLE_SLEEP_TIMEOUT=1800000`（30分）。左は n のまま | 放置時の右半分が約 0.4 mA → 約 5–10 µA。数週間 → 数か月 | 復帰に **数秒**（再起動＋分割再接続）、最初の打鍵は失われる。ZMK docs も "may take a few seconds to reconnect" |
| `CONFIG_ZMK_IDLE_TIMEOUT` | 30000（ZMK 既定） | 据え置き | LED/表示を 30 秒で消す。BLE は維持されるので遅延ゼロ | 短くすると LED がチラつくだけ |
| `CONFIG_ZMK_PM_SOFT_OFF` + `&soft_off` | **n**（`soft_off_wakers` は `cornix.dtsi` に配線済みだが未使用） | 任意。物理スイッチがあるので優先度は低い | 持ち運び時に約 5 µA。`&soft_off` は分割相手も一緒に落とす | 復帰は各半分のリセット押下（打鍵ウェイクは未検証）。鞄の中で誤爆の恐れ |
| `CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY` | **未指定 = ZMK 既定 30**（実験ビルドだけ `-D` で 0） | 左の conf に **0 を明示**（遅延優先）。待機時間が欲しければ 4–8 で再測定 | 実測中央値 −1.9 ms（`tests/bsim/README.md`）、実機の右半分もたつき解消（`scripts/log/README.md` §3b） | **右半分の待機電流が約 10 倍**（7.5 ms ごとに必ず受信）。これが右の電池寿命の最大要因 |
| `CONFIG_BT_CTLR_TX_PWR_*` | 左 `+8 dBm`（`cornix_left_defconfig`）、右 **0 dBm**（`tx8` ビルドは `-D` の実験） | 右は 0 dBm のまま | 右の TX 電流を上げない | リンクが切れるようなら右も +8 dBm（電池と引き換え） |
| DC/DC（`reg0` okay / `reg1` DCDC） | `nrf_e73.dtsi` の **DT で有効**（`CONFIG_BOARD_ENABLE_DCDC=n` でも効く） | 据え置き | 無線動作時の電流が 2〜3 割減 | UICR の `REGOUT0` に依存（下記 §2 の注意） |
| `CONFIG_ZMK_BATTERY_REPORTING` / `_REPORT_INTERVAL` | `y` / 左 60 秒 | 据え置き | 残量表示・LED・BAS 通知 | 60 秒なら電流は誤差。アイドル時は `battery.c` がタイマを止める |
| 分割ペリフェラル電池プロキシ | 左で `FETCHING` / `PROXY` = y | 据え置き | ホストから右半分の残量も見える | 分割リンクに 60 秒毎の GATT が少量増える |
| `CONFIG_ZMK_EXT_POWER` + WS2812 レール | 両半分 `y`、`EXT_POWER_TIMEOUT_MS=1000` | 据え置き | 電池運用時は表示 1 秒後にレールを切る | USB 時は `CORNIX_INDICATOR_LEDS_ON_USB=y` で点きっぱなし（USB 給電なので問題なし） |
| `BOARD_CORNIX_CHARGER` | `default y` だが **完全な死にコード** | **有効化しない**。できれば `pinmux.c` ごと削除 | 今は無害 | 生かすと P0.05 を GND に落とし、電池電圧が読めなくなる（§1） |

---

## 1. 「充電保護」— ファームでできること / ハードの仕事

### 1.1 `BOARD_CORNIX_CHARGER` の正体（結論: 死にコード、かつ Cornix では間違い）

- 定義は `boards/jzf/cornix/Kconfig`（`default y`）、使うのは `boards/jzf/cornix/pinmux.c` の 1 箇所だけ。
- その `pinmux.c` は全体が `#if (CONFIG_BOARD_CORNIX)` で囲まれている。**`CONFIG_BOARD_CORNIX` という
  シンボルは存在しない**（`board.yml` が定義するのは `cornix_left` / `cornix_right` / `cornix_ph_left`
  → `CONFIG_BOARD_CORNIX_LEFT` 等）。未定義シンボルは `#if` で 0 なので、中身は一行もビルドされていない。
  実ビルドの `.config` にも `CONFIG_BOARD_CORNIX_CHARGER=y` はあるが `CONFIG_BOARD_CORNIX` は無い。
- コードの出自は nrfmicro の `pinmux.c`。nrfmicro では **P0.05 = 充電 IC（TP4054 / MCP73831）の PROG ピン**で、
  Low 出力＝オンボード抵抗どおりの電流（約 100 mA）で充電、入力（ハイ Z）＝充電停止、という
  「充電電流の選択」であって充電イネーブルでもステータス入力でもない。分圧も 800 kΩ/2 MΩ で
  Cornix の 806 kΩ/2 MΩ と同型（Cornix はこの基板系譜の派生とみてよい）。
- ところが **Cornix では P0.05 は電池電圧の分圧タップ**である。RMK の `battery_adc_pin = "P0_05"` と、
  `nrf_e73.dtsi` の `io-channels = <&adc 3>`（AIN3 = P0.05）が一致し、実測ログの数字も合う（§3）。
  つまりこのコードが生きると **分圧の中点を GPIO で GND に落とす** ことになり、電池電圧が 0 になる。
  「充電が有効にならない」と思って `#if` を直す、というのが一番やってはいけない修正。
- したがって **Cornix の充電回路にファームから触れる配線は無い**。デバイスツリーにもソースにも
  充電イネーブル／`CHRG`・`STDBY` ステータス入力／専用 VBUS 検出ピンは存在しない。
  「充電中」判定は `zmk_usb_is_powered()`（＝ USB 給電の有無）だけで、実際には **充電完了を知る手段が無い**
  （`zmk-rgbled-widget` の充電色も「USB 給電中かつ 99% 未満」で緑パルス、というヒューリスティック）。

### 1.2 ハード側（ファームは一切関与しない）

- **過充電・充電電流・満充電停止（4.2 V CV）** = 基板上のリニア充電 IC（TP4054 / ME4054 / LTH7 系）。
  PROG 抵抗は固定。ファームからは止められないし、電流も変えられない。
- **過放電・過電流・短絡** = セル側の保護基板（PCM）。1S の完成パックなら通常 2.5–3.0 V で放電を遮断する。
  **保護基板の無い裸セルを使っている場合だけ**、深放電はファーム／運用でしか防げない。
- **逆流・挿抜** = 充電 IC と経路の FET/ダイオード。

### 1.3 ファームにできるのは次の 3 つだけ

1. **深放電を避ける** — 低電圧でスリープ／ソフトオフ／（一番確実なのは）物理スイッチを切る。
2. **残量を報告する** — `CONFIG_ZMK_BATTERY_REPORTING`、BAS、インジケータ LED。
3. **残量が少ないときに無線と LED を無理させない** — TX パワー、接続間隔、LED 点灯時間。

---

## 2. ZMK の省電力の仕組みと、今の設定

ZMK の低消費電力状態は 3 つ（`zmk/docs/docs/features/low-power-states.md`）。

| 状態 | 入り方 | 何が止まるか | 復帰 |
| --- | --- | --- | --- |
| **Idle** | `CONFIG_ZMK_IDLE_TIMEOUT`（既定 30 秒）無操作 | ディスプレイ・ライティング・電池サンプリング。**BLE 接続は維持** | 即座（遅延ペナルティ無し） |
| **Deep sleep** | `CONFIG_ZMK_SLEEP=y` かつ `CONFIG_ZMK_IDLE_SLEEP_TIMEOUT`（既定 900000 = 15 分）無操作、**かつ USB 給電が無いとき**（`app/src/activity.c`） | `sys_poweroff()`。BLE 全切断、外部電源 off、RAM 消失 | ウェイクアップ源（`wakeup-source` 付き kscan）で起動 → **数秒** |
| **Soft off** | `&soft_off` キー or 専用 GPIO（`CONFIG_ZMK_PM_SOFT_OFF=y` が必要） | 同じ `sys_poweroff()` | 指定した GPIO かリセットボタンのみ |

### 2.1 実ビルドの現在値（`.config` 実測）

左 `.build/led/cornix_left_full_p256m_debug/zephyr/.config`:

```
# CONFIG_ZMK_SLEEP is not set
# CONFIG_ZMK_PM_SOFT_OFF is not set
CONFIG_ZMK_IDLE_TIMEOUT=30000
CONFIG_ZMK_EXT_POWER=y
CONFIG_ZMK_BATTERY_REPORTING=y   CONFIG_ZMK_BATTERY_REPORT_INTERVAL=60
CONFIG_ZMK_BATTERY_REPORTING_FETCH_MODE_STATE_OF_CHARGE=y
CONFIG_BT_CTLR_TX_PWR_PLUS_8=y   (TX_PWR_DBM=8)
CONFIG_BT_PERIPHERAL_PREF_MIN_INT=6 / MAX_INT=12 / LATENCY=30 / TIMEOUT=400   (ZMK 既定)
CONFIG_ZMK_SPLIT_BLE_PREF_INT=6 / _PREF_LATENCY=0 / _PREF_TIMEOUT=400
CONFIG_ZMK_SPLIT_BLE_CENTRAL_BATTERY_LEVEL_{FETCHING,PROXY}=y
CONFIG_PM_DEVICE=y   CONFIG_ZMK_USB_LOGGING=y
# CONFIG_BOARD_ENABLE_DCDC is not set   ← だが DC/DC は DT 経由で有効（下記）
```

右 `.build/keymap/cornix_right_tx8_log/zephyr/.config`: `ZMK_SLEEP` / `ZMK_PM_SOFT_OFF` はやはり n、
`IDLE_TIMEOUT=30000`、`ZMK_EXT_POWER=y`、`BATTERY_REPORTING=y`（60 秒）、`PM_DEVICE` は **n**。

注意: この 2 つの `.config` に出てくる `PREF_LATENCY=0`（左）と `BT_CTLR_TX_PWR_PLUS_8`（右）は
ビルド時の `-D` 指定によるローカル実験値で、`zephyr/misc/generated/extra_kconfig_options.conf` に
そう残っている。**リポジトリにコミットされた conf には入っていない** ので、GitHub Actions が作る
ファームは右が 0 dBm、左の分割リンクは `PREF_LATENCY=30` になる。ここは明文化しておくべき差分。

### 2.2 DC/DC は「Kconfig は n だが DT で有効」

`CONFIG_BOARD_ENABLE_DCDC` は n のままだが、`nrf_e73.dtsi` が `&reg0 { status = "okay"; }` と
`&reg1 { regulator-initial-mode = <NRF5X_REG_MODE_DCDC>; }` を入れており、
`zephyr/soc/nordic/nrf52/soc.c` は **`CONFIG_REGULATOR` とは無関係に DT のプロパティを直接見て**
`nrf_power_dcdcen_set()` / `nrf_power_dcdcen_vddh_set()` を呼ぶ。生成された `zephyr.dts` でも
`reg1` に `regulator-initial-mode = <0x1>`、`reg0` が `status = "okay"` になっているので、
**両段とも DC/DC で動いている**（`CONFIG_REGULATOR is not set` は無関係）。

注意点: `reg0` は高電圧モード（VDDH に電池が入る構成）の段で、VDD の出力電圧は UICR の `REGOUT0` が
決める。RMK 側が `dcdc_reg0_voltage = "3V3"` で焼いた値をそのまま使っており、**ZMK は UICR を書かない**。
`nrfjprog --eraseall` などで UICR を消すと `REGOUT0` は既定の 1.8 V に戻るので、その場合は
書き戻しが要る（ここは実機で UICR を読んで確認しておく価値がある）。

### 2.3 いま作ってある機能との干渉（ここが重要）

- **`CONFIG_CORNIX_BLE_PAUSE_ON_USB`（BLE ゲート）** — USB 給電中しか動かない。ディープスリープは
  USB 給電中は発動しない（`activity.c` の `!is_usb_power_present()`）ので、**干渉しない**。
- **USB ログ（`CONFIG_ZMK_USB_LOGGING`）** — 同上。USB を挿している間は左は絶対に寝ない。
- **`CONFIG_CORNIX_REMOTE_BOOT` の 2400 bps（右半分をブートローダへ）** — 分割リンクが**繋がっている**
  ことが前提。右をディープスリープ／ソフトオフにすると効かない。フラッシュ前に一度打鍵して起こす必要がある。
- **右半分のウェイク遅延** — ユーザが一番嫌うところ。ディープスリープからの復帰は「GPIO で起動 →
  ブート → BLE スキャン → 分割再接続」で秒単位、しかも最初の 1〜数打鍵は失われる。
  一日中机で使う運用なら、右に短いスリープタイムアウトを入れるのは割に合わない。
- **ウェイクアップ源** — `cornix.dtsi` の `kscan0` に `wakeup-source` があり、`soft_off_wakers`
  （`zmk,soft-off-wakeup-sources`）にも `&kscan0` が登録済み。ディープスリープからの打鍵復帰はこれで効く。
  ソフトオフからの打鍵復帰も理屈上はこの経路だが、col2row マトリクスでの復帰は ZMK では
  専用 GPIO キー（`zmk,gpio-key-wakeup-trigger` + `extra-gpios`）が正攻法で、**現構成では未検証**。
  ソフトオフを入れるなら「復帰はリセットボタン」を前提にしたほうが安全。
- **右の `PM_DEVICE` は n** — `CONFIG_ZMK_SLEEP` を右で有効にすると Kconfig が自動で `PM_DEVICE=y` を
  引く（`ZMK_SLEEP` が `ZMK_PM_DEVICE_SUSPEND_RESUME` を select、`if ZMK_SLEEP` で `PM_DEVICE default y`）。
  RAM が数百バイト増える程度。

### 2.4 参考: RMK はどうしていたか

RMK の「スリープ」は電源断ではなく **接続パラメータの緩和**（`rmk/src/split/ble/central.rs`
`sleep_manager_task`）。タイムアウトで 20 ms 間隔 / latency 200（≒ 4 秒窓）に落とし、打鍵で
即座に元へ戻す。電源を切らないので復帰は 1 接続間隔（〜20 ms）で済む。
ZMK にはこれに相当する動的な接続パラメータ切り替えが無く、`CONFIG_ZMK_SPLIT_BLE_PREF_*` は
接続時の固定値。つまり **ZMK では「遅延」と「待機電流」を一本の定数で決め打ちするしかない**。
これが §5 の推奨がトレードオフの提示になる理由。

---

## 3. 電池電圧の測定 — RMK との一致と、ログの検算

`nrf_e73.dtsi`:

```dts
vbatt: vbatt {
    compatible = "zmk,battery-voltage-divider";
    io-channels = <&adc 3>;            /* AIN3 = P0.05 */
    output-ohms = <2000000>;           /* 2.0 MΩ（GND 側） */
    full-ohms  = <(2000000 + 806000)>; /* 2.806 MΩ（全体） */
};
```

- **ピン**: AIN3 = P0.05 = RMK の `battery_adc_pin = "P0_05"` と一致。
- **比**: ZMK は `full/output = 2806/2000 = 1.403` を掛ける。RMK は
  `adc_divider_measured = 2000` / `adc_divider_total = 2806` で同じ比。**完全に一致**。
- **検算**（`bvd_sample_fetch: ADC raw 3407 ~ 2994 mV => 4200 mV, Percent: 100`）:
  ドライバは gain 1/6・内部リファレンス 0.6 V・12 bit（`battery_voltage_divider.c`）なので
  フルスケール 3.6 V。`3407 × 3600 / 4096 = 2994 mV` ✓。
  `2994 × 2806 / 2000 = 4200.6 → 4200 mV` ✓。
  `lithium_ion_mv_to_pct(4200)` は 4200 以上で 100 を返す ✓。**計算は正しい**。
- **ただし USB 中の 100% は意味が無い**。測っているのは充電 IC の出力（CV 期の 4.2 V）であって
  セルの充電状態ではない。満充電でも充電中でも 100% と出る。残量を判断するなら
  **USB を抜いて数十秒後の値**を見ること。
- **% のカーブ**は `3450 mV = 0%`, `4200 mV = 100%` の直線近似（`battery_common.c`）。
  3.8 V → 47%、3.7 V → 34%。LiPo の放電曲線は平坦なので「中盤が速く減る」ように見えるが仕様どおり。
  `FETCH_MODE_STATE_OF_CHARGE` を使っていてもドライバ内で同じ関数が呼ばれるので式は同じ。
- **分圧の常時リーク**: `power-gpios` が無いので 2.806 MΩ が常時ぶら下がる。4.2 V で **約 1.5 µA**。
  ディープスリープ／ソフトオフ中も流れ続ける（nRF52840 の SYSTEM OFF 自体が数 µA なので無視できない割合）。
  **物理スイッチを切ったときだけゼロになる。**

---

## 4. 低電池時の挙動 — ZMK に組み込みのカットオフは無い

`app/src/battery.c` を読むと、やっているのは
「60 秒ごとにサンプル → 値が変わったら `zmk_battery_state_changed` を上げる → BAS に書く」だけ。
`app/Kconfig` の `ZMK_BATTERY_*` にも閾値・警告・遮断の類は無い。**0% になっても動き続ける。**

唯一の可視化は `zmk-rgbled-widget` の色分け（`cornix_indicator.conf`）:
`HIGH=80` 緑 / `LOW=20` 黄 / `CRITICAL=20` **赤点滅**。ただし電池 LED はイベント駆動で、
残量が変化したときに 2 秒光るだけ（USB 給電中は `CORNIX_INDICATOR_LEDS_ON_USB` で点きっぱなし）。
つまり **電池運用中に「気づく」確率は高くない**。

ZMK 流の選択肢と損得:

| 手段 | 実装コスト | 効き方 | 損 |
| --- | --- | --- | --- |
| **物理スイッチを切る** | 0 | 最も確実。分圧リーク 1.5 µA も止まる | 人間が覚えている必要がある |
| `&soft_off` キー（`CONFIG_ZMK_PM_SOFT_OFF=y`） | conf 1 行 + キーマップ 1 箇所 | 意図的に落とす。分割相手も一緒に落ちる（`split-peripheral-off-on-press`） | 復帰はリセット押下。誤爆防止に `hold-time-ms = <2000>` 推奨 |
| `CONFIG_ZMK_SLEEP` + 長めの `IDLE_SLEEP_TIMEOUT` | conf 2 行 | 忘れても勝手に落ちる | 復帰が数秒。低電圧とは無関係（時間だけで判定） |
| **自作リスナ**（`src/` にモジュールとして追加） | 20 行程度 | `zmk_battery_state_changed` を購読し、X% 未満が N 回続いたら `zmk_pm_soft_off()` | `ZMK_PM_SOFT_OFF` が前提。瞬間的な電圧降下での誤爆を避けるため連続回数で判定すること。USB 中は 100% なので誤爆はしない |

このリポジトリは既に ZMK モジュール（`zephyr/module.yml` + ルート `Kconfig` + `src/`）になっているので、
4 番目は `src/low_battery.c` を足して `CONFIG_CORNIX_LOW_BATTERY_SOFT_OFF_PCT` を作るだけで収まる。
ただし **物理スイッチがある以上、費用対効果は高くない**。入れるなら「LED を赤点滅させ続ける」
（閾値以下で `duration = 0` の永続表示にする）ほうが実用的かもしれない。

---

## 5. この機体への推奨セット

前提: 主に Windows PC に **USB 有線**、ときどき Mac に BLE、左に LED、右は電池のみ、
両半分に物理電源スイッチ、**遅延を強く嫌う**。

### 5.1 すぐ入れてよいもの

1. **`CONFIG_ZMK_SPLIT_BLE_PREF_LATENCY=0` を `config/` に明文化する**（現状は未指定＝30）。
   実験で `-D` 指定していた値をコミットする、という話。
   効果: bsim で中央値 −1.9 ms / p95 −1.3〜2.6 ms、実機の右半分もたつきも解消した実績。
   代償: 右半分は 7.5 ms ごとに必ず受信するので待機電流が約 10 倍（§5.3）。
   **遅延優先ならこれが正解**。待機時間も欲しくなったら `4`〜`8`（30〜60 ms の窓）で
   `scripts/log/analyze_latency.py` を取り直して妥協点を探すのが筋。
2. **右半分は 0 dBm のまま**。`+8 dBm` は TX 中の電流が約 3 倍になる。
   `scripts/log/README.md` §3 の症状が再発したときの切り札として温存する。
3. **左に `CONFIG_ZMK_SLEEP` は入れない**。USB 給電中は発動せず、BLE 運用中だけ効くが、
   その BLE 運用こそ「すぐ打ちたい」場面なので割に合わない。左は USB から給電されている時間が長く、
   電池消費自体が問題になりにくい。
4. **`CONFIG_ZMK_IDLE_TIMEOUT` は 30 秒のまま**。Idle は BLE を維持するので遅延コストがゼロ。
   ここを触っても LED の挙動が変わるだけ。
5. **`CONFIG_ZMK_BATTERY_REPORT_INTERVAL=60` のまま**。短縮は ADC とリンク traffic が増えるだけで得が無い。
6. **DC/DC は現状（DT で両段有効）を維持**。UICR `REGOUT0` の値だけ一度実機で確認しておく。

### 5.2 好みで入れるもの

7. **`CONFIG_ZMK_PM_SOFT_OFF=y` + キーマップに `&soft_off { hold-time-ms = <2000>; }`**。
   旅行・長期放置用。物理スイッチで代用できるので必須ではない。
   **復帰はリセットボタン前提**で運用すること（打鍵ウェイクは現構成では未検証）。
8. **右のみ `CONFIG_ZMK_SLEEP=y` + `CONFIG_ZMK_IDLE_SLEEP_TIMEOUT=1800000`（30 分）**。
   「席を離れて 30 分経ったら落ちる」なら復帰の数秒は許容しやすい。
   5 分・15 分（既定）は日常の中断で踏むので**やめたほうがいい**。
   入れるなら `&bootloader` のリモート投入（`scripts/flash.py --enter right`）が
   寝ている間は効かない点を覚えておくこと。
9. **低電池 LED の永続化**（閾値以下は赤点滅を消さない）。カットオフより先にこちらのほうが実害を減らす。

### 5.3 待機時間とウェイク遅延の目安（オーダー、300 mAh 想定）

実測ではなく nRF52840 の一般的な数字からの概算。相対比較として読むこと。

| 右半分の状態 | 概算平均電流 | 300 mAh での持ち | ウェイク遅延 |
| --- | --- | --- | --- |
| 打鍵中（7.5 ms / latency 0） | 0.6–1 mA | 約 2 週間 | — |
| アイドル接続維持（7.5 ms / **latency 0**） | 0.4–0.6 mA | **約 3 週間** | 0（常時接続） |
| アイドル接続維持（7.5 ms / **latency 30** = 最大 232 ms 窓） | 0.05–0.1 mA | **約 4–8 か月** | 理論上は次の接続イベント。ただし実機では体感で悪化した（原因未解明、§`scripts/log/README.md` 3b） |
| ディープスリープ / ソフトオフ | 5 µA + 分圧 1.5 µA ≒ 7 µA | 年単位（セルの自己放電が先に効く） | **数秒**＋初打鍵ロスト |
| 物理スイッチ OFF | 0 | セルの自己放電のみ | 電源投入からの起動時間 |

読み取り: **右半分の電池寿命を決めているのは `PREF_LATENCY` であってスリープではない**。
latency 30 → 0 の変更は「数か月 → 数週間」というオーダーの代償を払って数 ms を買う取引になる。
それでも本機は「毎日机で使い、週末に充電する」使い方なので、3 週間もてば実用上は十分で、
遅延を優先する判断は妥当。どうしても両立させたければ
`PREF_INT=12`（15 ms）+ `PREF_LATENCY=0` のような中間点を測るのが次の一手。

### 5.4 やらないこと

- `BOARD_CORNIX_CHARGER` を「直す」（§1.1。電池電圧が読めなくなる）。
- `CONFIG_ZMK_IDLE_TIMEOUT` の短縮、`BATTERY_REPORT_INTERVAL` の短縮。
- 左半分へのディープスリープ導入。
- ファームでの充電制御（配線が無いので不可能）。
