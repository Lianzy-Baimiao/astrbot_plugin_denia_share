/* 外观页：界面自定义（本页风格）+ 分享卡片设计器（实时预览）。
 *
 * 两块的存储不同，别混淆：
 * - 界面自定义 → POST appearance（插件数据目录 webui_appearance.json），
 *   只影响这个网页，立刻生效，不动插件配置。
 * - 卡片设计器 → POST config（RENDER_* 配置项），改的是真正发出去的卡片，
 *   走与配置页同一条保存链路（apply_updates → 运行时热更新渲染器）。
 *
 * 预览走 POST preview/sample：后端用离线示例数据渲染，不联网、不写解析记录。
 */

import {
  h,
  clear,
  card,
  toast,
  withBusy,
  switchControl,
  spinner,
  pill,
} from "../ui.js";

const HEX_RE = /^#[0-9a-fA-F]{6}$/;

const CARD_THEMES = [
  { value: "dark", label: "深色" },
  { value: "light", label: "浅色" },
];

const CARD_LAYOUTS = [
  { value: "standard", label: "standard 标准" },
  { value: "magazine", label: "magazine 杂志" },
  { value: "immersive", label: "immersive 沉浸" },
  { value: "feed", label: "feed 信息流" },
];

/** 卡片配置键 → 预览接口的外观参数名。 */
const OVERRIDE_MAP = {
  RENDER_THEME: "theme",
  RENDER_LAYOUT: "layout",
  RENDER_WIDTH: "width",
  RENDER_ACCENT_COLOR: "accent_color",
  RENDER_WATERMARK: "watermark",
  RENDER_DESC_MAX_LINES: "desc_max_lines",
  RENDER_SHOW_AVATAR: "show_avatar",
  RENDER_SHOW_PLAY_BUTTON: "show_play_button",
  RENDER_COVER_FULL_SIZE: "cover_full_size",
  RENDER_GRADIENT_TOP: "gradient_top",
  RENDER_GRADIENT_BOTTOM: "gradient_bottom",
};

export function createAppearanceView(ctx) {
  let container = null;
  let configValues = {};
  let cardDraft = {};
  let previewUrl = "";
  let previewMeta = null;
  let previewBusy = false;
  let previewTimer = 0;
  const elements = {};

  /* ---------------- 数据加载 ---------------- */

  async function load() {
    ctx.setHead("加载中…");
    const payload = await ctx.api.get("config");
    configValues = payload.values || {};
    cardDraft = {};
    ctx.setHead("");
  }

  function cardValue(key) {
    return Object.prototype.hasOwnProperty.call(cardDraft, key)
      ? cardDraft[key]
      : configValues[key];
  }

  function setCard(key, value) {
    if (value === configValues[key]) {
      delete cardDraft[key];
    } else {
      cardDraft[key] = value;
    }
    renderDirtyBar();
    schedulePreview();
  }

  /* ---------------- 预览 ---------------- */

  function previewOverrides() {
    const overrides = {};
    for (const [key, name] of Object.entries(OVERRIDE_MAP)) {
      if (Object.prototype.hasOwnProperty.call(cardDraft, key)) {
        overrides[name] = cardDraft[key];
      }
    }
    return overrides;
  }

  function schedulePreview() {
    window.clearTimeout(previewTimer);
    previewTimer = window.setTimeout(refreshPreview, 450);
  }

  async function refreshPreview() {
    if (previewBusy) {
      schedulePreview();
      return;
    }
    previewBusy = true;
    renderPreviewState();
    try {
      const payload = await ctx.api.post("preview/sample", {
        preview: previewOverrides(),
      });
      previewUrl = payload.data_url || "";
      previewMeta = payload;
    } catch (error) {
      previewUrl = "";
      previewMeta = { error: error.message || String(error) };
    } finally {
      previewBusy = false;
      renderPreviewState();
    }
  }

  function renderPreviewState() {
    if (!elements.previewBox) return;
    clear(elements.previewBox);
    if (previewBusy && !previewUrl) {
      elements.previewBox.appendChild(
        h("div", { class: "empty" }, [spinner(), " 渲染预览中…"]),
      );
    } else if (previewUrl) {
      const img = h("img", { src: previewUrl, alt: "卡片预览" });
      if (previewBusy) img.style.opacity = "0.55";
      elements.previewBox.appendChild(img);
    } else if (previewMeta && previewMeta.error) {
      elements.previewBox.appendChild(
        h("div", { class: "empty", text: `预览失败：${previewMeta.error}` }),
      );
    } else {
      elements.previewBox.appendChild(
        h("div", { class: "empty", text: "调整左侧任意参数即可生成预览" }),
      );
    }
    if (elements.previewMeta) {
      clear(elements.previewMeta);
      if (previewMeta && !previewMeta.error) {
        elements.previewMeta.appendChild(pill(`主题 ${previewMeta.theme}`, "primary"));
        elements.previewMeta.appendChild(pill(`布局 ${previewMeta.layout}`, "primary"));
        elements.previewMeta.appendChild(pill(`宽 ${previewMeta.width}px`, ""));
      }
    }
  }
  /* ---------------- 界面自定义 ---------------- */

  function seg(options, current, onPick, wrap) {
    return h(
      "div",
      { class: "seg", style: wrap ? { flexWrap: "wrap" } : null },
      options.map((item) =>
        h(
          "button",
          {
            type: "button",
            class: current === item.value ? "is-active" : "",
            onClick: () => onPick(item.value),
          },
          item.label,
        ),
      ),
    );
  }

  function renderInterfaceCard() {
    const p = ctx.prefs;

    const themeSeg = seg(
      [
        { value: "follow", label: "跟随面板" },
        { value: "light", label: "浅色" },
        { value: "dark", label: "深色" },
      ],
      p.theme_override,
      (v) => {
        ctx.savePrefs({ theme_override: v });
        render();
      },
    );

    const swatches = Object.entries(ctx.accentPresets)
      .filter(([key]) => key !== "")
      .map(([key, preset]) =>
        h("button", {
          type: "button",
          class: `swatch${p.accent === key ? " is-active" : ""}`,
          style: { background: `linear-gradient(135deg, ${preset.a}, ${preset.b})` },
          title: key,
          onClick: () => {
            ctx.savePrefs({ accent: p.accent === key ? "" : key });
            render();
          },
        }),
      );

    const picker = h("input", {
      type: "color",
      class: "swatch",
      title: "自定义颜色",
      value: HEX_RE.test(p.accent_custom) ? p.accent_custom : "#2f6fdd",
      style: { background: "none" },
      onInput: (event) => ctx.savePrefs({ accent_custom: event.target.value.toUpperCase() }),
      onChange: (event) => {
        ctx.savePrefs({ accent: "custom", accent_custom: event.target.value.toUpperCase() });
        render();
      },
    });

    const resetAccent = h(
      "button",
      {
        class: "btn tiny",
        type: "button",
        onClick: () => {
          ctx.savePrefs({ accent: "", accent_custom: "" });
          render();
        },
      },
      "默认",
    );

    const radiusSeg = seg(
      [
        { value: "s", label: "小" },
        { value: "m", label: "标准" },
        { value: "l", label: "大" },
      ],
      p.radius,
      (v) => {
        ctx.savePrefs({ radius: v });
        render();
      },
    );

    return card("界面自定义", "只影响本页面，即时生效并记住，不改动插件配置", [
      h("div", { class: "field" }, [
        h("div", { class: "field-label", text: "主题模式" }),
        themeSeg,
        h("p", { class: "field-hint", text: "跟随面板=与 AstrBot Dashboard 的亮/暗主题保持一致" }),
      ]),
      h("div", { class: "field" }, [
        h("div", { class: "field-label", text: "主题色" }),
        h("div", { class: "swatch-row" }, [...swatches, picker, resetAccent]),
        h("p", { class: "field-hint", text: "影响按钮、选中态与背景光晕；色块选预设，输入框自定义任意颜色" }),
      ]),
      h("div", { class: "field" }, [
        h("div", { class: "field-label", text: "圆角大小" }),
        radiusSeg,
      ]),
      h("div", { class: "field" }, [
        h("div", { class: "field-label", text: "布局密度" }),
        switchControl(p.compact, (on) => ctx.savePrefs({ compact: on }), "紧凑", "宽松"),
      ]),
      h("div", { class: "field" }, [
        h("div", { class: "field-label", text: "过渡动效" }),
        switchControl(p.animations, (on) => ctx.savePrefs({ animations: on }), "开启", "关闭"),
        h("p", { class: "field-hint", text: "关闭后所有过渡与骨架屏动画停用，低配设备更流畅" }),
      ]),
    ]);
  }
  /* ---------------- 卡片设计器 ---------------- */

  function colorField(label, key, hint) {
    const value = cardValue(key) || "";
    const input = h("input", {
      class: "input",
      type: "text",
      placeholder: "#RRGGBB，留空默认",
      value,
      spellcheck: "false",
      onInput: (event) => {
        const text = event.target.value.trim();
        event.target.style.borderColor = text && !HEX_RE.test(text) ? "var(--err)" : "";
      },
      onChange: (event) => {
        const text = event.target.value.trim();
        if (text && !HEX_RE.test(text)) {
          toast(`${label}：需要 #RRGGBB 格式`, "err");
          return;
        }
        setCard(key, text.toUpperCase());
      },
    });
    const picker = h("input", {
      type: "color",
      class: "input",
      title: "拾色器",
      value: HEX_RE.test(value) ? value : "#2f6fdd",
      style: { width: "38px", height: "34px", padding: "2px", cursor: "pointer", flex: "0 0 auto" },
      onInput: (event) => {
        input.value = event.target.value.toUpperCase();
        setCard(key, event.target.value.toUpperCase());
      },
    });
    const clearBtn = h(
      "button",
      {
        class: "btn tiny",
        type: "button",
        onClick: () => {
          input.value = "";
          setCard(key, "");
        },
      },
      "默认",
    );
    return h("div", { class: "field" }, [
      h("div", { class: "field-label", text: label }),
      h("div", { class: "field-body" }, [input, picker, clearBtn]),
      hint ? h("p", { class: "field-hint", text: hint }) : null,
    ]);
  }

  function numField(label, key, { min = 0, max = 999, unit = "", hint = "" } = {}) {
    const input = h("input", {
      class: "input",
      type: "number",
      min: String(min),
      max: String(max),
      value: String(cardValue(key) ?? min),
      style: { maxWidth: "130px" },
      onChange: (event) => {
        const raw = Number(event.target.value);
        if (!Number.isFinite(raw)) return;
        const clamped = Math.max(min, Math.min(max, Math.round(raw)));
        event.target.value = String(clamped);
        setCard(key, clamped);
      },
    });
    return h("div", { class: "field" }, [
      h("div", { class: "field-label", text: label }),
      h("div", { class: "field-body" }, [input, unit ? h("span", { class: "unit", text: unit }) : null]),
      hint ? h("p", { class: "field-hint", text: hint }) : null,
    ]);
  }

  function switchField(label, key, onText, offText, hint) {
    return h("div", { class: "field" }, [
      h("div", { class: "field-label", text: label }),
      switchControl(Boolean(cardValue(key)), (on) => setCard(key, on), onText, offText),
      hint ? h("p", { class: "field-hint", text: hint }) : null,
    ]);
  }

  function renderCardDesigner() {
    const themeSeg = seg(CARD_THEMES, cardValue("RENDER_THEME"), (v) => {
      setCard("RENDER_THEME", v);
      render();
    });
    const layoutSeg = seg(
      CARD_LAYOUTS,
      cardValue("RENDER_LAYOUT"),
      (v) => {
        setCard("RENDER_LAYOUT", v);
        render();
      },
      true,
    );

    const watermarkInput = h("input", {
      class: "input",
      type: "text",
      maxlength: "12",
      placeholder: "留空则不显示水印",
      value: cardValue("RENDER_WATERMARK") ?? "",
      onChange: (event) => setCard("RENDER_WATERMARK", event.target.value.trim().slice(0, 12)),
    });

    return card("卡片设计器", "改的是真正发出去的分享卡片，保存后立即生效", [
      switchField("卡片渲染", "RENDER_ENABLED", "启用卡片", "关闭卡片", "还需在发送模式中勾选卡片；关闭仅停发卡片，不影响文本和富媒体"),
      h("div", { class: "field" }, [
        h("div", { class: "field-label", text: "卡片主题" }),
        themeSeg,
      ]),
      h("div", { class: "field" }, [
        h("div", { class: "field-label", text: "卡片布局" }),
        layoutSeg,
      ]),
      numField("卡片宽度", "RENDER_WIDTH", { min: 520, max: 1080, unit: "px（520~1080）" }),
      colorField("自定义强调色", "RENDER_ACCENT_COLOR", "留空=每个平台用自己的品牌色；填后所有卡片统一强调色"),
      h("div", { class: "field" }, [
        h("div", { class: "field-label", text: "卡片水印文字" }),
        h("div", { class: "field-body" }, [watermarkInput]),
        h("p", { class: "field-hint", text: "卡片右下角的水印，留空则不显示，最长 12 个字符" }),
      ]),
      numField("正文最大行数", "RENDER_DESC_MAX_LINES", { min: 0, max: 12, unit: "行（0=按布局默认 4~6 行）" }),
      switchField("作者头像", "RENDER_SHOW_AVATAR", "显示", "隐藏"),
      switchField(
        "封面播放按钮",
        "RENDER_SHOW_PLAY_BUTTON",
        "显示",
        "隐藏",
        "视频封面中央的毛玻璃播放按钮；关掉后封面更干净",
      ),
      switchField("封面展示", "RENDER_COVER_FULL_SIZE", "完整", "裁切", "完整=不裁切封面，长图完整铺在卡片顶部"),
      colorField("背景渐变 · 顶部色", "RENDER_GRADIENT_TOP"),
      colorField("背景渐变 · 底部色", "RENDER_GRADIENT_BOTTOM"),
    ]);
  }
  /* ---------------- 预览卡片 / 保存栏 / 装配 ---------------- */

  function renderPreviewCard() {
    elements.previewBox = h("div", { class: "preview-box" });
    elements.previewMeta = h("div", { class: "preview-meta" });
    const refreshBtn = h(
      "button",
      {
        class: "btn tiny",
        type: "button",
        onClick: () => refreshPreview(),
      },
      "重新渲染",
    );
    return card("实时预览", "离线示例数据，不联网、不产生解析记录", [
      elements.previewBox,
      elements.previewMeta,
      h("div", { class: "preview-meta" }, [refreshBtn]),
    ]);
  }

  function renderDirtyBar() {
    if (!elements.dirtyHost) return;
    clear(elements.dirtyHost);
    const keys = Object.keys(cardDraft);
    if (!keys.length) return;
    const saveBtn = h(
      "button",
      { class: "btn primary", type: "button", onClick: () => saveCardConfig(saveBtn) },
      `保存 ${keys.length} 项修改`,
    );
    const discardBtn = h(
      "button",
      {
        class: "btn",
        type: "button",
        onClick: () => {
          cardDraft = {};
          render();
          schedulePreview();
        },
      },
      "放弃",
    );
    elements.dirtyHost.appendChild(
      h("div", { class: "savebar" }, [
        h("span", { class: "card-note", text: `卡片样式有 ${keys.length} 项未保存的修改` }),
        h("span", { class: "spacer" }),
        discardBtn,
        saveBtn,
      ]),
    );
  }

  async function saveCardConfig(button) {
    const restore = withBusy(button, true, "保存中…");
    try {
      const payload = await ctx.api.post("config", { values: cardDraft });
      const errors = payload.errors || [];
      if (errors.length) {
        toast(`部分保存失败：${errors.join("；")}`, "err");
      } else {
        toast(`已保存 ${(payload.changed || []).length} 项，卡片样式即时生效`, "ok");
      }
      configValues = payload.values || configValues;
      cardDraft = {};
      render();
      schedulePreview();
    } catch (error) {
      toast(`保存失败：${error.message || error}`, "err");
    } finally {
      restore();
    }
  }

  function render() {
    if (!container) return;
    clear(container);
    const left = h(
      "div",
      { style: { display: "flex", flexDirection: "column", gap: "var(--gap)" } },
      [renderInterfaceCard(), renderCardDesigner()],
    );
    const right = h("div", { class: "preview-stage" }, [renderPreviewCard()]);
    container.appendChild(h("div", { class: "appearance-grid" }, [left, right]));
    elements.dirtyHost = h("div");
    container.appendChild(elements.dirtyHost);
    renderDirtyBar();
    renderPreviewState();
  }

  return {
    async mount(node) {
      container = node;
      await load();
      render();
      refreshPreview();
    },
    async refresh() {
      await load();
      render();
      refreshPreview();
    },
    unmount() {
      window.clearTimeout(previewTimer);
    },
  };
}
