# 英文法 段階制ロック学習サイト v3 — 設計書

**作成日**: 2026-05-06  
**版**: v3  
**対応教材**: 英文法_PDFまとめ（17章、224項目、672演習）

---

## 0. 概要

PDF教材（問題・解答 各3段階 + 解説スライド）を投入し、以下を一発で自動生成する完全自動パイプラインの設計。

### 入出力仕様

**入力**：
- `第XX章/` フォルダ配下の問題・解答PDF（演習・習熟・習得 各3段階）
- `第XX章/成果物/` の解説スライド PPTX

**出力**：
- `HP/grammar_v3.html` — 単一 HTML（約2.5MB、全機能内蔵）
- `HP/data/curriculum.json` — 構造化マスタデータ（約2.5MB）
- `HP/main.py` — FastAPI バックエンド
- `HP/build_curriculum.py` — PDF抽出ビルダー
- `HP/inline_curriculum.py` — JSON→HTML埋め込みツール

---

## 1. 教材データ分析

### 1-1. ファイル統計

| 項目 | 数量 |
| --- | --- |
| 総章数 | 17章 |
| 総項目数 | 224項目（推定） |
| 総PDF数 | 672 PDF（224項目 × 3段階） |
| 総PPTX数 | 224 PPTX（各項目の解説スライド） |

### 1-2. 章別構成

```
第01章: 48 PDF | 16 PPTX
第02章: 30 PDF | 10 PPTX
第03章: 30 PDF | 10 PPTX
第04章: 42 PDF | 14 PPTX
第05章: 30 PDF | 10 PPTX
第06章: 36 PDF | 12 PPTX
第07章: 30 PDF | 10 PPTX
第08章: 60 PDF | 20 PPTX
第09章: 51 PDF | 17 PPTX
第10章: 57 PDF | 19 PPTX
第11章: 51 PDF | 17 PPTX
第12章: 30 PDF | 10 PPTX
第13章: 45 PDF | 15 PPTX
第14章: 39 PDF | 13 PPTX
第15章: 48 PDF | 16 PPTX
第16章: 24 PDF | 8 PPTX
第17章: 21 PDF | 7 PPTX
```

### 1-3. ファイル名パターン

**問題・解答 PDF**：
```
問題_h_eng_gra_XX_YYY『項目名』（演習|習熟|習得）.pdf
解答_h_eng_gra_XX_YYY『項目名』（演習|習熟|習得）.pdf
```

抽出正規表現：
```regex
r'(問題|解答)_h_eng_gra_(\d{2})_(\d{3})『(.+?)』（(演習|習熟|習得)）\.pdf$'
```

**解説スライド PPTX**：
```
第XX章_YYY_項目名_内容.pptx
```

**Word問題集 DOCX**（成果物フォルダ）：
```
第XX章_YYY_項目名_[演習|習熟|習得]_[問題|解答].docx
```

### 1-4. PDF テキスト抽出品質

✅ **確認済み**: `pdftotext -layout` で高品質なテキスト抽出が可能
- 日本語対応
- レイアウト保持
- ヘッダー・フッター含む（後処理で除去）

**サンプル抽出結果**（第01章_001『「現在形」と「現在進行形」』演習）：
```
英文法 通常学習編              第１章「時制」       1-2 演習プリント                得点
「現在形」と「現在進⾏形」                                          100
Ａ問題
１     次の英語を日本語にしなさい。（各２点・計４点）
(1)   I play soccer every day.         私は毎日，                     。
(2)   I am playing soccer now.         私は今，                      。
```

---

## 2. 納品物の構成と役割

### 2-1. ビルダーパイプライン

```
HP/build_curriculum.py
  ├─ parse_filename() → 章・項目番号を抽出
  ├─ extract_pdf_text() → pdftotext で問題・解答を抽出
  ├─ extract_pptx_slides() → python-pptx で スライドテキストを抽出
  ├─ build_chapter() → 並列処理で各章をビルド
  │   └─ 出力: HP/data/ch01.json … ch17.json
  └─ merge_curriculum() → ch*.json を統合
      └─ 出力: HP/data/curriculum.json

実行: python HP/build_curriculum.py all
所要時間: 約17秒（全1344ファイル）
```

### 2-2. データ構造（curriculum.json）

```json
{
  "metadata": {
    "title": "英文法 通常学習編",
    "version": "v3",
    "chapters": 17,
    "items": 224,
    "generated_at": "2026-05-06T12:34:56Z"
  },
  "chapters": [
    {
      "id": 1,
      "name": "第01章",
      "title": "時制",
      "color": "#FF6B6B",
      "items": [
        {
          "id": "item_001",
          "chapter": 1,
          "item_num": 1,
          "name": "「現在形」と「現在進行形」",
          "slides": [
            { "type": "text", "content": "..." },
            { "type": "image", "base64": "..." }
          ],
          "stages": [
            {
              "stage": 1,
              "stage_name": "演習",
              "problem_text": "１ 次の英語を日本語にしなさい...",
              "answer_text": "【解答】(1) 私は毎日サッカーをします...",
              "q_count": 12,
              "pdf_path": "../第01章/問題_h_eng_gra_01_001『...』（演習）.pdf"
            },
            {
              "stage": 2,
              "stage_name": "習熟",
              ...
            },
            {
              "stage": 3,
              "stage_name": "習得",
              ...
            }
          ]
        }
      ]
    }
  ]
}
```

### 2-3. HTML学習サイト（grammar_v3.html）

**仕様**：
- 単一 HTML ファイル（CSS/JS内蔵）
- curriculum.json を `<script id="curriculum-data">` に埋め込み
- SPA（Single Page Application）で画面遷移

**4階層UI**：

```
┌─ ログイン画面 ─────────────────────┐
│ ユーザーID（demo） / PW（demo）    │
└────────────────────────────────────┘
           ↓
┌─ サイドバー + 章ビュー ────────────┐
│ 【第01章】時制                     │
│   └─ 001. 「現在形」と「現在進⾏形」 │
│   └─ 003. 「不変の真理」の時制は？   │
│   ...                              │
│ 【第02章】助動詞                   │
│   ...                              │
└────────────────────────────────────┘
           ↓
┌─ 項目ビュー（4カード） ────────────┐
│ 解説スライド｜演習｜習熟｜習得      │
│ [解放]      |[施行]|[🔒]  |[🔒]   │
└────────────────────────────────────┘
           ↓
┌─ ワークシート（採点） ─────────────┐
│ 問題文（pre 等幅）                 │
│ 解答（<details> 折りたたみ）       │
│ 自己採点フォーム：                 │
│   正解数 / 全問数 を入力           │
│   → 得点率自動計算                 │
│   → 「採点を確定する」 ボタン      │
└────────────────────────────────────┘
           ↓
┌─ 結果画面 ────────────────────────┐
│ 得点: 85/100 (85%)                │
│ ✅ 合格！ → 次段階を解放           │
│ または                             │
│ ❌ 不合格（85%に達してません）     │
│ 📄 プリント 🔄 もう一度            │
└────────────────────────────────────┘
```

**段階制ロック仕様**：

```
演習: 常に解放
  ↓ (正解率 85% 以上で初合格)
習熟: 解放
  ↓ (正解率 85% 以上で初合格)
習得: 解放
  ↓ (正解率 85% 以上で初合格)
完了: 項目クリア 🏆
```

**プリント機能**：

```
@media print {
  .print-sheet {
    表題: 「英文法 通常学習編 ／ 第NN章 章名 ／ 習得プリント」
    項目: 「項目名」
    名前: 「名前：______」
    日付: 「日付：　　年　　月　　日」
    点数: 「得点：___ ／ 全問数 （　　 %）」
    本文: 問題テキスト全文
  }
}
```

### 2-4. FastAPI バックエンド（main.py）

**エンドポイント**：

| メソッド | 路径 | 用途 |
| --- | --- | --- |
| GET | `/ping` | ヘルスチェック → "ok" |
| GET | `/` | HTML サービス → grammar_v3.html |
| GET | `/v1` | 旧版互換 → grammar.html（非推奨） |
| GET | `/data/curriculum.json` | マスタデータ配信 |
| GET | `/pdf/{filepath}` | PDF静的配信 |
| POST | `/saveStageStatus` | 進捗保存 |
| GET | `/getStageStatus` | 進捗取得 |

**データベース設定**：

```python
DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "sqlite:///./hp.db"  # デフォルト: SQLite
)
# Render 環境では PostgreSQL に自動切替
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)
```

**テーブル**：

```sql
-- ユーザー
users (uid, name, email)

-- 進捗（旧版互換）
progress (uid, stage, score, passed_at)

-- 弱点分析（旧版互換）
weaknesses (uid, item_id, count)

-- ステージ進捗（v3 新設）
stage_status (
  uid, item_id, stage,
  best_score, attempts, passed, passed_at, last_at
)
```

---

## 3. 実装ロードマップ

### 3-1. ビルダー実装（build_curriculum.py）

```python
# 構成
1. パラメータ定義
   - CHAPTER_TITLES = { 1: "時制", 2: "助動詞", ... }
   - CHAPTER_COLORS = { 1: "#FF6B6B", ... }
   - MAX_WORKERS = 8

2. テキスト抽出関数
   - extract_pdf_text(filepath) → str
   - extract_pptx_slides(filepath) → list[dict]
   - parse_filename(filename) → (章, 項目, 名前, 段階)

3. ビルド関数
   - build_chapter(chapter_num) → dict
   - merge_curriculum(*ch_jsons) → dict

4. CLIエントリ
   - python HP/build_curriculum.py all
     → HP/data/ch01.json … ch17.json → curriculum.json
   - python HP/build_curriculum.py 01 03
     → 指定章のみビルド
```

実行時間目安: 17秒（全1344ファイル並列処理）

### 3-2. HTML実装（grammar_v3_template.html）

```html
<!-- 構成 -->
1. <head>
   - title: "英文法 通常学習編"
   - スタイル（CSS内蔵）
   - curriculum-data スクリプト

2. <body>
   - ログイン画面
   - サイドバー + 章ビュー
   - 項目ビュー（4カード）
   - ワークシート
   - 結果画面
   - プリント用領域

3. <script>
   - getItemStageState() → ロック判定
   - saveStageResult() → 進捗保存
   - printSheet() → プリント生成
   - 各画面の SPA ロジック
```

### 3-3. バックエンド実装（main.py）

```python
# 構成
1. FastAPI app 初期化
2. SQLAlchemy Models定義
3. CRUD操作実装
4. 以下のエンドポイント
   - /ping
   - /saveStageStatus
   - /getStageStatus
   - 旧版エンドポイント互換
5. 静的ファイル配信設定
```

---

## 4. 実装スケジュール

### フェーズ1: ビルダー作成＆実行（【プロンプト②】）
- [ ] build_curriculum.py 作成
- [ ] inline_curriculum.py 作成
- [ ] 全章ビルド実行
- [ ] curriculum.json 生成確認

### フェーズ2: HTML作成（【プロンプト③】）
- [ ] grammar_v3_template.html 作成
- [ ] JSON埋め込み実行
- [ ] grammar_v3.html 生成確認
- [ ] jsdom テスト実行

### フェーズ3: バックエンド作成（【プロンプト④】）
- [ ] main.py 作成
- [ ] DB テーブル設定
- [ ] エンドポイント実装
- [ ] /tmp でローカル起動テスト

### フェーズ4: GitHub + Render 設定（【プロンプト⑤】— 既済）
- [x] Procfile / runtime.txt / requirements.txt / .gitignore / README.md 作成済

---

## 5. リスク評価と対策

| リスク | 影響 | 対策 |
| --- | --- | --- |
| PDF テキスト抽出失敗（画像PDF） | 一部項目が `/pdf/` リンクのみ表示 | 既に確認済み：抽出品質は高い |
| HTML ファイルサイズ超過 | 初回ロード遅延 | gzip圧縮で約500KBに圧縮予定 |
| Render Free プランでスリープ | サイト応答遅延 | Starter ($7/月) アップグレード推奨 |
| PPTX スライド数が多い | JSON サイズ肥大化 | base64 圧縮 + lazy loading |

---

## 6. 成功基準

- [x] PDF抽出品質確認（✓ 完了）
- [ ] curriculum.json 生成: 17章 × 224項目 の構造化データ完成
- [ ] grammar_v3.html: 2.5MB以下、ブラウザで動作確認
- [ ] FastAPI: すべてのエンドポイント200 OK
- [ ] Render デプロイ: HTTPS で公開可能

---

**次ステップ**: 【プロンプト②】build_curriculum.py 作成・実行

