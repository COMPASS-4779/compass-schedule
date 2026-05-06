# 英文法学習サイト v2 設計書

「段階制ロック」と「項目別学習」を中核にした英文法学習システム。
既存 `grammar.html` の SPA 構造（FastAPI + 単一HTMLクライアント）を踏襲しつつ、
17章 224項目の `演習 → 習熟 → 習得` 3段階反復学習に再設計する。

---

## 1. 全体像

### 1.1 採用方針（確認済）

| 項目 | 採用案 |
| --- | --- |
| コンテンツ配置 | JS内に直接埋め込み（grammar.htmlと同様の `CURRICULUM` 定数を拡張） |
| 反復ロジック | 段階制ロック（演習 85% → 習熟解放 → 85% → 習得解放 → 85%で項目クリア） |
| 問題データ生成 | 既存PDFから自動抽出（pdfplumber等）→ JSON化 → JSへ埋め込み |
| 納品物 | 本設計書 + プロトタイプHTML（`grammar_v2.html`） |

### 1.2 アーキテクチャ概要

```
┌─────────────────────────────────────────┐
│  ブラウザ（grammar_v2.html）             │
│  ┌───────────────────────────────────┐  │
│  │  単一HTMLファイル                 │  │
│  │   ・UI（Login / 章一覧 / 項目     │  │
│  │     詳細 / 解説 / 問題演習 / 結果） │  │
│  │   ・CURRICULUM 定数（章・項目・   │  │
│  │     スライド・問題データ）         │  │
│  │   ・段階制ロック判定ロジック       │  │
│  │   ・進捗管理（API & localStorage）│  │
│  └───────────┬───────────────────────┘  │
└──────────────┼──────────────────────────┘
               │ HTTPS (REST-like GET)
               ▼
┌─────────────────────────────────────────┐
│  FastAPI（main.py を拡張）              │
│   ・/getProgress, /saveProgress         │
│   ・/getStageStatus, /saveStageStatus   │ ← 新設
│   ・/getWeaknesses, /saveWeaknesses     │
│   ・/getUsers, /saveUser                │
│   ↓                                     │
│  PostgreSQL / SQLite                    │
└─────────────────────────────────────────┘
```

### 1.3 既存資産との関係

| 既存資産 | 新HPでの扱い |
| --- | --- |
| `grammar.html`（基礎/標準/応用/受験の4レベル制） | UI / CSS / API通信処理を流用、CURRICULUMだけ全面差し替え |
| `第NN章/スライド_*.pptx` | 内容を抽出してスライドJSON化（章カラー・例文に変換） |
| `第NN章/問題_*.pdf` × 224項目 × 3段階 | 内容を抽出して問題JSON化（4択・並び替え・空所補充） |
| `第NN章/解答_*.pdf` × 224項目 × 3段階 | 解説欄に変換 |
| `main.py`（FastAPI + SQLAlchemy） | テーブル追加・エンドポイント追加で流用 |

---

## 2. データモデル

### 2.1 学習コンテンツ（クライアント埋め込み）

```javascript
const CURRICULUM = {
  meta: {
    version: "2.0",
    passMark: 85,        // 合格ライン
    quizQCount: 10       // 1セッション設問数
  },
  chapters: [
    {
      id: "ch01",
      num: 1,
      title: "時制",
      color: "#4caf50",
      items: [
        {
          id: "ch01_001",
          num: "001",
          name: "「現在形」と「現在進行形」",
          // 解説スライド（PPTXから抽出）
          slides: [
            {
              title: "現在形と現在進行形の違い",
              content: "■ 現在形：習慣・状態\n■ 現在進行形：今まさに進行中の動作",
              examples: [
                { en: "I play tennis.",
                  ja: "私はテニスをします（習慣）" },
                { en: "I am playing tennis now.",
                  ja: "私は今テニスをしています（進行中）" }
              ]
            }
            // 3〜5枚
          ],
          // 3段階の問題セット（PDFから抽出）
          stages: {
            enshu: {                       // 演習
              label: "演習",
              questions: [ /* 10問 */ ]
            },
            shujuku: {                     // 習熟
              label: "習熟",
              questions: [ /* 10問 */ ]
            },
            shutoku: {                     // 習得
              label: "習得",
              questions: [ /* 10問 */ ]
            }
          }
        }
        // ... 16項目
      ]
    }
    // ... 17章
  ]
};
```

### 2.2 問題オブジェクト（質問1問の標準形）

```javascript
{
  q: "次の英文の空所に入る適切なものを選びなさい：The book ___ by him.",
  type: "mc",                                  // mc | fill | order
  ch: ["wrote", "is wrote", "was written", "writing"],
  a: 2,                                        // 正解インデックス
  exp: "受動態は be動詞 + 過去分詞。主語が単数で過去なので was written。",
  cat: "受動態の基本"                            // 弱点分析カテゴリ
}
```

### 2.3 進捗データ（バックエンド永続化）

新テーブル `stage_status` を追加：

| カラム | 型 | 説明 |
| --- | --- | --- |
| id | int PK | |
| uid | str | ユーザーID |
| item_id | str | `ch01_001` 等の項目ID |
| stage | str | `enshu` / `shujuku` / `shutoku` |
| best_score | int | 過去最高得点（0〜100） |
| attempts | int | 挑戦回数 |
| passed | bool | 85%以上達成済か |
| passed_at | str | 合格日時（YYYY/MM/DD HH:MM） |
| last_at | str | 最終挑戦日時 |

既存の `progress` テーブルはセッション単位の生ログとして残し、
`stage_status` は段階制ロックの判定用集計テーブルとして使う。

### 2.4 クライアントキャッシュ（localStorage）

| キー | 用途 |
| --- | --- |
| `grm_session` | ログイン中のユーザーID |
| `grm_stage_v2` | バックエンド未接続時のフォールバック進捗 |
| `grm_pref_v2` | 表示設定（フォントサイズ等） |

---

## 3. 段階制ロックの動作仕様

### 3.1 ロック状態の遷移

```
┌─────────┐   合格(≥85%)   ┌─────────┐   合格(≥85%)   ┌─────────┐   合格(≥85%)
│  解説    │ ────────────▶ │  演習    │ ────────────▶ │  習熟    │ ────────────▶ ★ 項目クリア
│ (常時可) │   <85%で      │          │   <85%で      │  習得    │   <85%は再挑戦
└─────────┘   再挑戦       └─────────┘   再挑戦       └─────────┘
                                  │                         │
                                  │ ロック中                │ ロック中
                                  ▼                         ▼
                            🔒 習熟・習得ボタン       🔒 習得ボタン
                            は押下できない             は押下できない
```

### 3.2 ボタン状態判定（擬似コード）

```javascript
function getStageState(itemId) {
  const s = userStageStatus[itemId] || {};
  return {
    enshu:   { unlocked: true,
               passed: s.enshu?.passed   ?? false },
    shujuku: { unlocked: !!s.enshu?.passed,
               passed:   s.shujuku?.passed ?? false },
    shutoku: { unlocked: !!s.shujuku?.passed,
               passed:   s.shutoku?.passed ?? false }
  };
}

function isItemCleared(itemId) {
  return getStageState(itemId).shutoku.passed;
}
```

### 3.3 不合格時のフロー

`PASS_MARK = 85` 未満なら結果画面で次の選択肢を提示：

1. **間違えた問題だけ再挑戦**（今回間違えた問題のみで再小テスト→このミニ判定では合格扱いにせず、参考情報として表示）
2. **同じ段階を最初から再挑戦**（10問新シャッフル）
3. **解説に戻る**

`best_score` のみ更新し、`passed` は 85% 達成時のみ true にセット。

### 3.4 出題の重複対策

各段階に 10〜30問のプールを保持し、`pickQuestions(pool, 10)` で：

- 過去の不正解傾向（`weaknesses` テーブル）から優先
- 直近1セッションで出した問題は重複しないように抽出
- プール 10問未満なら全問出題

---

## 4. UI設計（画面遷移）

### 4.1 主要ビュー

| ビューID | 役割 |
| --- | --- |
| `view-login` | ID/PW ログイン・新規登録 |
| `view-dashboard` | 進捗サマリ・続きから・章ジャンプ |
| `view-chapter` | 章内 項目一覧（クリア状況をバッジで可視化） |
| `view-item` | 項目詳細（解説／演習／習熟／習得 4つのカード） |
| `view-slides` | 解説スライド（左右ナビ・音声読み上げ任意） |
| `view-quiz` | 問題演習（10問1セット・進捗バー） |
| `view-result` | 採点結果・解説・次アクション |
| `view-admin-*` | ユーザー／進捗／弱点管理（既存流用） |

### 4.2 サイドバー

```
📚 英語学習サイト
├ 🏠 ダッシュボード
├ 📖 第01章 時制              ▷ クリア 6/16
├ 📖 第02章 受動態            ▷ クリア 3/10
├ 📖 第03章 助動詞            ▷ クリア 0/10
│  …（17章）
└ ⚙️ 管理者メニュー（admin時のみ）
```

### 4.3 項目詳細画面（key画面）

```
┌──────────────────────────────────────────┐
│  ← 第02章へ戻る                          │
│  📖 第02章-001  「受動態」とは？          │
│  ───────────────────────────────────    │
│  ┌─────────┐ ┌─────────┐ ┌─────────┐ ┌─────────┐
│  │ 解説     │ │ 演習     │ │ 🔒習熟   │ │ 🔒習得  │
│  │ 4スライド│ │ 10問     │ │ 10問     │ │ 10問    │
│  │ [開く]   │ │ [挑戦]   │ │ 演習合格 │ │ 習熟合格│
│  │          │ │ 85% ✅  │ │ で解放   │ │ で解放  │
│  └─────────┘ └─────────┘ └─────────┘ └─────────┘
│                                          │
│  💡 全段階85%以上で「項目クリア」🏆       │
└──────────────────────────────────────────┘
```

### 4.4 結果画面の例

```
┌──────────────────────────────────────────┐
│  📝 第02章-001 演習 結果                 │
│  ──────────────────────────────────     │
│        90点 / 100点                     │
│        ✅ 合格！習熟が解放されました      │
│  ──────────────────────────────────     │
│  ✗ 第3問 The car ___ by him.            │
│    あなた: was wrote                    │
│    正解  : was written                  │
│    解説  : 過去分詞 written を使う        │
│                                          │
│  [習熟に挑戦] [もう一度演習] [項目に戻る] │
└──────────────────────────────────────────┘
```

---

## 5. PDF → JSON 変換パイプライン

### 5.1 前提

- 入力：`英文法_PDFまとめ/第NN章/問題_h_eng_gra_NN_XXX『...』（演習|習熟|習得）.pdf` × 約672ファイル
- 入力：`英文法_PDFまとめ/第NN章/解答_h_eng_gra_NN_XXX『...』（演習|習熟|習得）.pdf` × 約672ファイル
- 出力：`HP/data/curriculum.json`（最終的にHTMLにインライン展開）

### 5.2 抽出スクリプトの構成

```
build_curriculum.py
├ scan_pdfs()         … 章・項目・段階の3次元インデックス作成
├ extract_text(pdf)   … pdfplumberでテキスト化（フォーマット崩れ補正）
├ parse_questions()   … 問題本文から大問→小問→選択肢/空欄を抽出
│   ├ regex_mc()           4択・2択を見つける
│   ├ regex_blank()        ___ や（  ）形式の空所を見つける
│   └ regex_translate()    和訳・英訳の指示を見つける
├ parse_answers()     … 解答PDFから回答と解説を抽出
├ merge()             … 問題と解答を突合してQAペア化
├ classify_type()     … type=mc/fill/order に振り分け
├ to_quiz_format()    … 標準形（q/ch/a/exp/cat）に変換
└ build_curriculum_json()
```

### 5.3 取得粒度

- スライドは PPTX から `slide.title / paragraph / examples` を機械抽出
- PPTXがない章（例：第01章）は問題PDFの「例題」部分からスライド再構成
- 問題は **多肢選択（mc）優先で抽出**。フリー記述（並べ替え・英作文）は段階の補足問題（採点対象外の演習）として表示

### 5.4 抽出後の手当て

PDFからの自動抽出は精度70〜85%が現実的なため、以下の二段構えで品質を担保：

1. **自動抽出**（一次データ）
2. **抜き取りレビュー**（章ごとに1項目を目視）→ 抽出ロジックを微調整して再生成
3. **CSVで一括校正**（各問題のq/ch/a/expをスプレッドシートで眺め、校正後に再ビルド）

---

## 6. ファイル構成（HPフォルダ）

```
HP/
├ grammar.html               （旧版 v1：保管）
├ grammar_v2.html            ★ 新版（プロトタイプ）
├ main.py                    （FastAPI、エンドポイント追加で流用）
├ requirements.txt
├ Procfile.txt
├ data/
│  ├ curriculum.json         ★ 全コンテンツのマスタ
│  ├ chapters.json           （章メタ：番号・名前・色）
│  └ items_index.json        （項目ID索引）
├ build/
│  ├ build_curriculum.py     ★ PDF→JSON ビルダ
│  ├ inline_curriculum.py    ★ JSON を HTML に埋め込むツール
│  └ qa_review.py            ★ 抽出品質確認スクリプト
├ 設計書_grammar_v2.md        ★ 本ファイル
└ curriculum_items.json      （章×項目の生インデックス）
```

最終ビルド時は `inline_curriculum.py` で `data/curriculum.json` を `grammar_v2.html` 内の `<script id="curriculum-data" type="application/json">…</script>` に埋め込み、単一HTMLとして配布する。

---

## 7. API追加仕様（main.py 拡張）

新規エンドポイント：

```
GET /getStageStatus?uid=...
  → { ok, status: { "ch02_001": {
        enshu:   { best:90, attempts:2, passed:true,  passed_at:"2026/05/04" },
        shujuku: { best:70, attempts:1, passed:false                          },
        shutoku: { best:0,  attempts:0, passed:false                          }
      }, ... }}

GET /saveStageStatus?uid=...&item_id=...&stage=...&score=...&passed=true|false
  → { ok, saved:true, unlocked_next: "shujuku" | null }
```

`saveStageStatus` 側で：

- `best_score = max(prev_best, score)`
- 初めて 85% 達成したときに `passed=true, passed_at=now` を付与
- 次段階の解放判定結果を返す（クライアントUIの即時反映用）

---

## 8. 開発ステップ（実装ロードマップ）

| フェーズ | 内容 | 想定工数 |
| --- | --- | --- |
| P1 | 設計書ご確認・調整 | （本ステップ） |
| P2 | プロトタイプHTML（grammar_v2.html）でロック動作確認 | 0.5d |
| P3 | `build_curriculum.py` 設計・1章分でPoC | 1〜2d |
| P4 | 全17章 PDF→JSON 一括ビルド & 品質チェック | 2〜3d |
| P5 | スライド抽出（PPTX→slides JSON） | 1〜2d |
| P6 | API拡張（stage_status テーブル追加） | 0.5d |
| P7 | 結合テスト・UI微調整・デプロイ | 1d |

合計：6〜10営業日が目安。

---

## 9. プロトタイプ（grammar_v2.html）の含有要件

本納品プロトタイプには以下を含める：

- 上記UI/フロー全画面の動作実装
- サンプルデータ：
  - 第02章「受動態」3項目（001 / 003 / 005）× 演習・習熟・習得 各5問
  - 第03章「助動詞」1項目（001 canの基本用法）× 演習・習熟・習得 各5問
- 段階制ロック（85%）が正しく動くこと
- 進捗は localStorage に保存（バックエンド未接続でも動作）
- API接続コードは残し、URL設定で本番切替可

未実装項目（サンプル外）はサイドバーに灰色で「準備中」表示する。

---

## 10. リスクと対策

| リスク | 対策 |
| --- | --- |
| PDFからの問題抽出精度 | 章ごとにPoC→人手レビュー→スクリプト改善のループ |
| 224項目×3段階×10問=6720問の量 | 1章ずつ段階的にビルド、CSV校正フローを準備 |
| クライアント側のJSサイズ肥大 | 章単位で動的import or 圧縮配信を検討（初期は単一HTMLで可） |
| 既存ユーザーの進捗データ | v1の `progress` テーブルから `stage_status` への移行スクリプトを併設 |
| 採点が機械的にしづらい問題（英作文等） | 採点対象外の「補足演習」として、解答例とともに表示 |

---

以上、本設計に沿ってプロトタイプ `grammar_v2.html` を同フォルダに作成します。
