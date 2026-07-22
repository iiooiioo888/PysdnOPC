/**
 * OPC 前端國際化 (i18n) 模組
 * 
 * 提供輕量級的翻譯功能，支援繁體中文 (zh-TW) 和英文 (en)。
 * 
 * 使用方式：
 * ```tsx
 * import { useI18n, I18nProvider } from './lib/i18n';
 * 
 * function MyComponent() {
 *   const { t, locale, setLocale } = useI18n();
 *   return <div>{t('common.save')}</div>;
 * }
 * 
 * // 在 App 頂層包裝
 * <I18nProvider defaultLocale="zh-TW">
 *   <App />
 * </I18nProvider>
 * ```
 */

import React, { createContext, useContext, useState, useCallback, useMemo } from 'react';
import { zhTW } from '../locales/zh-TW';
import { en } from '../locales/en';

export type Locale = 'zh-TW' | 'en';

const LOCALE_TABLES: Record<Locale, Record<string, string>> = {
  'zh-TW': zhTW,
  'en': en,
};

/**
 * 翻譯函數
 * @param key 翻譯鍵名
 * @param params 替換參數
 * @returns 翻譯後的字串
 */
export function translate(
  key: string,
  locale: Locale = 'zh-TW',
  params?: Record<string, string | number>
): string {
  const table = LOCALE_TABLES[locale] || LOCALE_TABLES['zh-TW'];
  let text = table[key];
  
  // 如果找不到翻譯，嘗試英文後備
  if (text === undefined && locale !== 'en') {
    text = LOCALE_TABLES['en'][key];
  }
  
  // 如果仍找不到，返回鍵名本身
  if (text === undefined) {
    return key;
  }
  
  // 替換參數
  if (params) {
    for (const [paramKey, paramValue] of Object.entries(params)) {
      text = text.replace(new RegExp(`\\{${paramKey}\\}`, 'g'), String(paramValue));
    }
  }
  
  return text;
}

interface I18nContextValue {
  locale: Locale;
  setLocale: (locale: Locale) => void;
  t: (key: string, params?: Record<string, string | number>) => string;
}

const I18nContext = createContext<I18nContextValue | null>(null);

interface I18nProviderProps {
  children: React.ReactNode;
  defaultLocale?: Locale;
}

/**
 * i18n 提供者元件
 * 包裝應用程式以提供翻譯功能
 */
export function I18nProvider({ children, defaultLocale = 'zh-TW' }: I18nProviderProps) {
  const [locale, setLocale] = useState<Locale>(() => {
    // 嘗試從 localStorage 讀取使用者偏好
    if (typeof window !== 'undefined') {
      const saved = localStorage.getItem('opc-locale');
      if (saved === 'zh-TW' || saved === 'en') {
        return saved;
      }
    }
    return defaultLocale;
  });

  const handleSetLocale = useCallback((newLocale: Locale) => {
    setLocale(newLocale);
    if (typeof window !== 'undefined') {
      localStorage.setItem('opc-locale', newLocale);
    }
  }, []);

  const t = useCallback(
    (key: string, params?: Record<string, string | number>) => {
      return translate(key, locale, params);
    },
    [locale]
  );

  const value = useMemo(
    () => ({ locale, setLocale: handleSetLocale, t }),
    [locale, handleSetLocale, t]
  );

  return <I18nContext.Provider value={value}>{children}</I18nContext.Provider>;
}

/**
 * 使用 i18n 的 Hook
 * @returns i18n 上下文值
 */
export function useI18n(): I18nContextValue {
  const context = useContext(I18nContext);
  if (!context) {
    // 如果沒有 Provider，返回預設值
    return {
      locale: 'zh-TW',
      setLocale: () => {},
      t: (key: string, params?: Record<string, string | number>) =>
        translate(key, 'zh-TW', params),
    };
  }
  return context;
}

export default I18nProvider;
