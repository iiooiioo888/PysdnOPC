/**
 * Simple locale helper for non-React contexts.
 * Provides a default `t` function using zh-TW locale.
 */
import { zhTW } from '../locales/zh-TW';

type LocaleTable = Record<string, string>;

let currentLocale: LocaleTable = zhTW;

export function setLocaleTable(table: LocaleTable): void {
  currentLocale = table;
}

export function t(key: string, params?: Record<string, string | number>): string {
  let text = currentLocale[key] ?? key;
  if (params) {
    for (const [k, v] of Object.entries(params)) {
      text = text.replace(new RegExp(`\\{${k}\\}`, 'g'), String(v));
    }
  }
  return text;
}
