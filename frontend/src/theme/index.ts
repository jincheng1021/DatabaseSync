/**
 * 设计系统 → Naive UI 主题映射。
 * 从 tokens.css 的 CSS 变量运行时取值（不重复硬编码），暗色为默认。
 */
import type { GlobalThemeOverrides } from 'naive-ui'

function v(name: string, fallback: string): string {
  const val = getComputedStyle(document.documentElement).getPropertyValue(name).trim()
  return val || fallback
}

export function buildThemeOverrides(): GlobalThemeOverrides {
  const common = {
    primaryColor: v('--Semantic-Color-accent', '#7D3BED'),
    primaryColorHover: v('--Semantic-Color-accent-hover', '#9952F7'),
    primaryColorPressed: v('--Semantic-Color-accent-hover', '#9952F7'),
    infoColor: v('--Semantic-Color-info', '#21D4ED'),
    successColor: v('--Semantic-Color-success', '#0FBA82'),
    warningColor: v('--Semantic-Color-warning', '#F59E0A'),
    errorColor: v('--Semantic-Color-error', '#F5405E'),
    bodyColor: v('--Semantic-Color-bg-base', '#0A0A0A'),
    cardColor: v('--Semantic-Color-bg-card', '#141417'),
    popoverColor: v('--Semantic-Color-bg-elevated', '#17171C'),
    modalColor: v('--Semantic-Color-bg-elevated', '#17171C'),
    borderColor: v('--Semantic-Color-border', '#262629'),
    dividerColor: v('--Semantic-Color-border', '#262629'),
    textColor1: v('--Semantic-Color-text-primary', '#FAFAFA'),
    textColor2: v('--Semantic-Color-text-secondary', '#A1A1A6'),
    textColor3: v('--Semantic-Color-text-tertiary', '#75757A'),
    borderRadius: '6px',
    borderRadiusSmall: '4px',
    fontSize: '14px',
    fontSizeMedium: '14px',
    fontSizeSmall: '13px',
  }
  return {
    common,
    Button: { borderRadiusMedium: '6px', borderRadiusSmall: '6px' },
    Card: { borderRadius: '8px' },
    Dialog: { borderRadius: '12px' },
    Drawer: { borderRadius: '12px' },
    Popover: { borderRadius: '12px' },
  }
}
