"""Generate the Phase 1-A client-info checklist as a PDF (development items only)."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.enums import TA_CENTER, TA_LEFT, TA_RIGHT
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import (
    BaseDocTemplate,
    Frame,
    KeepTogether,
    PageBreak,
    PageTemplate,
    Paragraph,
    Spacer,
    Table,
    TableStyle,
)

FONT_REG = "HeiseiKakuGo-W5"
FONT_MIN = "HeiseiMin-W3"
pdfmetrics.registerFont(UnicodeCIDFont(FONT_REG))
pdfmetrics.registerFont(UnicodeCIDFont(FONT_MIN))

ACCENT = colors.HexColor("#1F3A68")
ACCENT_LIGHT = colors.HexColor("#EEF2F8")
GREY = colors.HexColor("#555555")
GREY_LIGHT = colors.HexColor("#F2F4F8")
WHITE = colors.white


def get_styles() -> dict[str, ParagraphStyle]:
    base = getSampleStyleSheet()
    styles: dict[str, ParagraphStyle] = {}

    styles["title"] = ParagraphStyle(
        "title", parent=base["Normal"], fontName=FONT_REG, fontSize=22,
        textColor=ACCENT, alignment=TA_CENTER, leading=30, spaceAfter=6,
    )
    styles["subtitle"] = ParagraphStyle(
        "subtitle", parent=base["Normal"], fontName=FONT_REG, fontSize=13,
        textColor=GREY, alignment=TA_CENTER, leading=18, spaceAfter=4,
    )
    styles["meta"] = ParagraphStyle(
        "meta", parent=base["Normal"], fontName=FONT_MIN, fontSize=9,
        textColor=GREY, alignment=TA_CENTER, leading=13,
    )
    styles["h1"] = ParagraphStyle(
        "h1", parent=base["Normal"], fontName=FONT_REG, fontSize=15,
        textColor=ACCENT, leading=20, spaceBefore=14, spaceAfter=8,
        borderPadding=4, borderColor=ACCENT, borderWidth=0,
    )
    styles["h2"] = ParagraphStyle(
        "h2", parent=base["Normal"], fontName=FONT_REG, fontSize=12,
        textColor=ACCENT, leading=16, spaceBefore=10, spaceAfter=4,
    )
    styles["body"] = ParagraphStyle(
        "body", parent=base["Normal"], fontName=FONT_MIN, fontSize=10,
        textColor=colors.black, leading=15, spaceAfter=4,
    )
    styles["body_small"] = ParagraphStyle(
        "body_small", parent=base["Normal"], fontName=FONT_MIN, fontSize=9,
        textColor=colors.black, leading=13, spaceAfter=2,
    )
    styles["callout"] = ParagraphStyle(
        "callout", parent=base["Normal"], fontName=FONT_REG, fontSize=10,
        textColor=ACCENT, leading=15, alignment=TA_LEFT,
        leftIndent=8, rightIndent=8, spaceBefore=4, spaceAfter=4,
    )
    styles["table_header"] = ParagraphStyle(
        "table_header", parent=base["Normal"], fontName=FONT_REG, fontSize=9.5,
        textColor=WHITE, alignment=TA_CENTER, leading=12,
    )
    styles["table_cell"] = ParagraphStyle(
        "table_cell", parent=base["Normal"], fontName=FONT_MIN, fontSize=9,
        textColor=colors.black, leading=12, alignment=TA_LEFT,
    )
    styles["table_cell_center"] = ParagraphStyle(
        "table_cell_center", parent=base["Normal"], fontName=FONT_MIN, fontSize=9,
        textColor=colors.black, leading=12, alignment=TA_CENTER,
    )
    styles["footer"] = ParagraphStyle(
        "footer", parent=base["Normal"], fontName=FONT_MIN, fontSize=8,
        textColor=GREY, alignment=TA_CENTER, leading=11,
    )
    return styles


def page_decorator(canvas, doc):
    canvas.saveState()
    # Header bar (top)
    canvas.setFillColor(ACCENT)
    canvas.rect(0, A4[1] - 8 * mm, A4[0], 4 * mm, fill=1, stroke=0)
    # Footer
    canvas.setFont(FONT_MIN, 8)
    canvas.setFillColor(GREY)
    canvas.drawCentredString(
        A4[0] / 2, 10 * mm,
        f"Phase 1-A 開発キックオフ：ご提供情報チェックリスト    -  {doc.page}  -",
    )
    canvas.restoreState()


def make_table(data: list[list[str]], col_widths: list[float],
                header: bool = True, styles: dict | None = None) -> Table:
    s = styles or get_styles()
    rows = []
    for r_idx, row in enumerate(data):
        new_row = []
        for c_idx, cell in enumerate(row):
            if header and r_idx == 0:
                new_row.append(Paragraph(cell, s["table_header"]))
            else:
                style = s["table_cell_center"] if c_idx == 0 and len(row) > 2 else s["table_cell"]
                new_row.append(Paragraph(cell, style))
        rows.append(new_row)

    table = Table(rows, colWidths=col_widths, repeatRows=1 if header else 0)
    cmds = [
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("LEFTPADDING", (0, 0), (-1, -1), 5),
        ("RIGHTPADDING", (0, 0), (-1, -1), 5),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B8C2D2")),
    ]
    if header:
        cmds.append(("BACKGROUND", (0, 0), (-1, 0), ACCENT))
        for r in range(2, len(data), 2):
            cmds.append(("BACKGROUND", (0, r), (-1, r), GREY_LIGHT))
    else:
        for r in range(1, len(data), 2):
            cmds.append(("BACKGROUND", (0, r), (-1, r), GREY_LIGHT))
    table.setStyle(TableStyle(cmds))
    return table


def make_callout(text: str, styles: dict) -> Table:
    p = Paragraph(text, styles["callout"])
    t = Table([[p]], colWidths=[170 * mm])
    t.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, -1), ACCENT_LIGHT),
        ("LEFTPADDING", (0, 0), (-1, -1), 10),
        ("RIGHTPADDING", (0, 0), (-1, -1), 10),
        ("TOPPADDING", (0, 0), (-1, -1), 8),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
        ("LINEBEFORE", (0, 0), (0, -1), 3, ACCENT),
    ]))
    return t


def build() -> Path:
    out_path = Path(__file__).parent / "Phase1-A_ご提供情報チェックリスト.pdf"
    doc = BaseDocTemplate(
        str(out_path), pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm,
        topMargin=18 * mm, bottomMargin=18 * mm,
        title="Phase 1-A ご提供情報チェックリスト",
        author="開発担当",
    )
    frame = Frame(doc.leftMargin, doc.bottomMargin,
                   doc.width, doc.height, id="normal")
    doc.addPageTemplates([PageTemplate(id="default", frames=frame,
                                         onPage=page_decorator)])

    s = get_styles()
    story: list = []

    # --- Cover ---
    story.append(Spacer(1, 30 * mm))
    story.append(Paragraph("Phase 1-A 開発キックオフ", s["title"]))
    story.append(Spacer(1, 4 * mm))
    story.append(Paragraph("ご提供情報チェックリスト", s["subtitle"]))
    story.append(Spacer(1, 2 * mm))
    story.append(Paragraph("楽天 × Shopify 在庫・需要予測・自動発注システム", s["meta"]))

    story.append(Spacer(1, 25 * mm))
    cover_info = [
        ["版数", "v1.0"],
        ["作成日", datetime(2026, 5, 11).strftime("%Y年%m月%d日")],
        ["対象", "Phase 1-A（取り込み + マスター在庫管理）"],
        ["範囲", "開発に必要な提供情報のみ"],
    ]
    t = Table(cover_info, colWidths=[40 * mm, 100 * mm])
    t.setStyle(TableStyle([
        ("FONTNAME", (0, 0), (-1, -1), FONT_MIN),
        ("FONTSIZE", (0, 0), (-1, -1), 10),
        ("BACKGROUND", (0, 0), (0, -1), ACCENT),
        ("TEXTCOLOR", (0, 0), (0, -1), WHITE),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#B8C2D2")),
        ("LEFTPADDING", (0, 0), (-1, -1), 8),
        ("RIGHTPADDING", (0, 0), (-1, -1), 8),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("FONTNAME", (0, 0), (0, -1), FONT_REG),
    ]))
    t.hAlign = "CENTER"
    story.append(t)

    story.append(Spacer(1, 30 * mm))
    story.append(Paragraph(
        "本書は Phase 1-A 開発の着手にあたり、クライアント様からご提供いただく "
        "情報を整理したものです。チェックリストとしてご活用ください。",
        s["meta"],
    ))

    story.append(PageBreak())

    # --- 概要 ---
    story.append(Paragraph("はじめに", s["h1"]))
    story.append(Paragraph(
        "Phase 1-A 開発を円滑に進めるため、開発工程ごとにクライアント様からご提供いただきたい情報を整理しました。"
        "<br/><br/>"
        "本書は <b>開発に必要な情報のみ</b> を対象とし、契約・法務に関する書面はスコープ外としています。"
        "<br/><br/>"
        "情報のご提供タイミングは A〜D の4段階に分けています。"
        "特に <b>A. 着手前必須項目</b> は、W1 開始に直結するため最優先でのご対応をお願いいたします。",
        s["body"],
    ))
    story.append(Spacer(1, 4 * mm))

    overview_data = [
        ["区分", "タイミング", "主な内容"],
        ["A", "着手前必須<br/>（〜W1開始まで）", "GCP環境 / 楽天RMS API / Shopify Admin API"],
        ["B", "W1（要件確定・設計）", "商品マスタ / 運用情報 / 既存システム連携"],
        ["C", "W2〜W3（実装・テスト）", "テスト環境 / サンプルデータ / 通知・デザイン"],
        ["D", "W4（受入・本番切替）", "検収責任者 / 本番ドメイン / 切替判断"],
    ]
    story.append(make_table(overview_data, [15 * mm, 45 * mm, 110 * mm], styles=s))

    story.append(Spacer(1, 8 * mm))
    story.append(make_callout(
        "★ 楽天RMS API の利用申請が未完了の場合は、申請から1〜2週間を要します。"
        "他項目より先に申請状況のご確認をお願いいたします。",
        s,
    ))

    story.append(PageBreak())

    # --- A. 着手前必須 ---
    story.append(Paragraph("A. 着手前必須（〜W1開始まで）", s["h1"]))
    story.append(Paragraph(
        "GCP環境構築・API疎通確認に必要です。これらが揃わないと W1 を開始できません。",
        s["body"],
    ))

    story.append(Paragraph("A-1. GCP環境", s["h2"]))
    data_a1 = [
        ["#", "項目", "内容・備考"],
        ["1", "GCPプロジェクトID", "新規作成 または 既存プロジェクトのご指定"],
        ["2", "課金アカウント", "プロジェクトへの紐付けが完了していること"],
        ["3", "利用リージョン", "推奨：asia-northeast1（東京）"],
        ["4", "開発者へのIAM権限付与", "Owner または Editor + Secret Manager Admin"],
        ["5", "既存BigQueryの<br/>プロジェクト/データセット名", "日次exportの宛先として"],
        ["6", "組織ポリシー制約の有無", "VPC強制・Public IP禁止等があれば事前共有"],
    ]
    story.append(make_table(data_a1, [10 * mm, 55 * mm, 105 * mm], styles=s))

    story.append(Paragraph("A-2. 楽天RMS API", s["h2"]))
    data_a2 = [
        ["#", "項目", "取得方法・備考"],
        ["7", "RMSサービスシークレット<br/>（serviceSecret）", "RMS → 拡張サービス → API設定"],
        ["8", "ライセンスキー（licenseKey）", "同上"],
        ["9", "店舗URL / shopUrl", "例：https://www.rakuten.co.jp/yourshop/"],
        ["10", "利用可能なAPI権限", "注文API（getOrder / searchOrder）の権限ON確認"],
        ["11", "楽天ペイ運用切替の有無", "旧楽天注文API or 楽天ペイ注文APIで挙動が異なる"],
        ["12", "API利用申請の状況", "<b>未申請の場合は申請から1〜2週間要するため最優先</b>"],
    ]
    story.append(make_table(data_a2, [10 * mm, 55 * mm, 105 * mm], styles=s))

    story.append(Paragraph("A-3. Shopify Admin API", s["h2"]))
    data_a3 = [
        ["#", "項目", "取得方法・備考"],
        ["13", "ストアドメイン", "xxx.myshopify.com 形式"],
        ["14", "Custom App の<br/>Admin API access token", "Shopify管理画面 → アプリ → アプリ開発"],
        ["15", "必要スコープの付与", "read_orders / read_products / read_inventory /<br/>read_locations / write_inventory（Phase 1-Bで使用）"],
        ["16", "Webhook設定権限", "アプリ作成権限があれば自動設定可"],
        ["17", "API版数の希望", "既定：最新安定版（例 2025-04）"],
    ]
    story.append(make_table(data_a3, [10 * mm, 55 * mm, 105 * mm], styles=s))

    story.append(PageBreak())

    # --- B. W1 ---
    story.append(Paragraph("B. W1（要件確定・設計）で必要", s["h1"]))
    story.append(Paragraph(
        "設計確定のためのヒアリングが発生します。打ち合わせ1〜2回で収集予定です。",
        s["body"],
    ))

    story.append(Paragraph("B-1. 商品マスタ情報", s["h2"]))
    data_b1 = [
        ["#", "項目", "形式・用途"],
        ["18", "マスターSKU命名規則", "テキスト or 既存リスト（命名規則確定）"],
        ["19", "既存マスターSKU一覧", "CSV / スプレッドシート（初期投入データ）"],
        ["20", "楽天SKU ⇔ Shopify<br/>variant ID 対応表", "CSV（あれば。マッピング初期データ）"],
        ["21", "JANコード一覧", "CSV（あれば。バーコード照合用）"],
        ["22", "商品カテゴリ・属性体系", "テキスト（Phase 2分析準備）"],
        ["23", "想定SKU件数（現状・1年後）", "概算（性能設計の根拠）"],
    ]
    story.append(make_table(data_b1, [10 * mm, 55 * mm, 105 * mm], styles=s))
    story.append(Paragraph(
        "※ 既存情報がない場合は弊社で雛形提案いたします（運用都合での命名規則変更も可能です）。",
        s["body_small"],
    ))

    story.append(Paragraph("B-2. 運用情報", s["h2"]))
    data_b2 = [
        ["#", "項目", "内容"],
        ["24", "管理画面ログインユーザー一覧", "氏名・メールアドレス・役割"],
        ["25", "想定同時利用人数", "性能想定の根拠"],
        ["26", "エラー通知先メールアドレス", "障害通知・日次レポート宛先"],
        ["27", "営業時間・受注ピーク時間帯", "スケール設計・メンテ時間決定"],
        ["28", "現状の在庫同期手順", "クロスモールの何をどう使っているか"],
        ["29", "倉庫・出荷拠点", "単一前提だが念のため確認"],
        ["30", "キャンセル・返品の業務フロー", "補償イベントの仕様確定"],
    ]
    story.append(make_table(data_b2, [10 * mm, 55 * mm, 105 * mm], styles=s))

    story.append(Paragraph("B-3. 既存システム連携", s["h2"]))
    data_b3 = [
        ["#", "項目", "内容"],
        ["31", "クロスモール画面の<br/>スクリーンショット（5〜10枚）<br/>または短時間の画面共有", "現状機能の把握"],
        ["32", "既存BigQueryのスキーマ", "既存テーブルとの整合性確認"],
        ["33", "既存社内システム（ERP・基幹）<br/>との連携要否", "Phase 1-Aスコープ外確認"],
    ]
    story.append(make_table(data_b3, [10 * mm, 55 * mm, 105 * mm], styles=s))

    story.append(PageBreak())

    # --- C. W2-W3 ---
    story.append(Paragraph("C. W2〜W3（実装・テスト）で必要", s["h1"]))
    story.append(Paragraph(
        "実装中に発生する確認事項です。チャットでの随時対応で十分です。",
        s["body"],
    ))
    data_c = [
        ["#", "項目", "内容"],
        ["34", "楽天のテスト店舗の有無", "あればテストAPI接続情報、<br/>なければ本番店舗で読み取り限定のテスト"],
        ["35", "Shopify Development Store", "テスト用ストア。Partner経由で無償発行可能"],
        ["36", "テスト用注文の作成可否", "楽天本番でテスト注文を1〜2件作成可能か"],
        ["37", "サンプル注文データ", "過去30日分の注文を1ファイル（CSV / JSON）"],
        ["38", "通知文言の確認", "エラー通知・日次レポートの文面・粒度"],
        ["39", "管理画面のロゴ・カラー指定", "任意。指定なければ標準デザイン"],
    ]
    story.append(make_table(data_c, [10 * mm, 55 * mm, 105 * mm], styles=s))

    # --- D. W4 ---
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph("D. W4（受入・本番切替）で必要", s["h1"]))
    data_d = [
        ["#", "項目", "内容"],
        ["40", "検収責任者", "受入試験の判断者"],
        ["41", "本番ドメイン（管理画面用）", "例 inventory.example.co.jp<br/>（DNS設定はクライアント側）"],
        ["42", "SSL証明書方針", "Google managed SSL利用で問題ないか"],
        ["43", "管理画面のIP制限要否", "オフィスIPからのみ等の要件があれば"],
        ["44", "本番Webhook登録のタイミング", "切替日の合意"],
        ["45", "初期在庫データの投入方針", "クロスモール現在値スナップショット<br/>または棚卸結果"],
        ["46", "並行稼働期間の運用ルール", "クロスモールと本システムの責務分担"],
    ]
    story.append(make_table(data_d, [10 * mm, 55 * mm, 105 * mm], styles=s))

    story.append(PageBreak())

    # --- 認証情報受け渡し ---
    story.append(Paragraph("認証情報の受け渡し方法（推奨）", s["h1"]))
    story.append(Paragraph(
        "セキュリティ上、API認証情報・各種シークレットは以下のいずれかの方法でのお受け渡しをお願いいたします。",
        s["body"],
    ))
    cred_data = [
        ["推奨度", "方法"],
        ["★★★", "クライアント様ご自身が Secret Manager に直接登録、<br/>IAM権限のみ弊社に付与（流出リスク最小）"],
        ["★★☆", "パスワード保護PDF（パスワードは別チャネル送付）"],
        ["★☆☆", "1Password / Bitwarden 等の共有リンク（一時アクセス）"],
        ["非推奨", "平文メール・チャット直貼り"],
    ]
    story.append(make_table(cred_data, [20 * mm, 150 * mm], styles=s))
    story.append(Spacer(1, 4 * mm))
    story.append(make_callout(
        "特に楽天RMSのライセンスキーは流出時の影響が大きいため、Secret Manager 直接登録方式を強く推奨いたします。"
        "弊社が登録代行する場合は、登録後にクライアント様側でローテーション可能な手順をご案内します。",
        s,
    ))

    # --- 最優先 ---
    story.append(Spacer(1, 6 * mm))
    story.append(Paragraph("最優先でご対応いただきたい項目", s["h1"]))
    story.append(Paragraph(
        "下記3点が揃った時点で <b>W1 着手可能</b> となります。",
        s["body"],
    ))
    priority_data = [
        ["優先度", "項目", "理由"],
        ["1", "楽天RMS API の利用申請状況確認（#12）", "未申請なら1〜2週間要するため最優先"],
        ["2", "GCPプロジェクトの作成と弊社へのIAM権限付与（#1, #4）", "GCPがないと環境構築できません"],
        ["3", "Shopify Custom App の作成と access token 発行（#14）", "概ね30分作業"],
    ]
    story.append(make_table(priority_data, [18 * mm, 90 * mm, 62 * mm], styles=s))

    story.append(Spacer(1, 10 * mm))
    story.append(Paragraph(
        "ご不明点がございましたらお気軽にお問い合わせください。",
        s["body"],
    ))
    story.append(Paragraph(
        "本書に記載のない事項についても、開発進行上必要となった場合はその都度ご相談させていただきます。",
        s["body_small"],
    ))

    story.append(Spacer(1, 15 * mm))
    story.append(Paragraph("— 以上 —", s["meta"]))

    doc.build(story)
    return out_path


if __name__ == "__main__":
    path = build()
    print(f"Generated: {path}")
