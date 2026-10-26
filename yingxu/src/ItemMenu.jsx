import React from 'react';
import { MoreHorizontal } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { Menu } from './ui/Menu.jsx';
// The "…" row menu. Rows are ui/Menu MenuItem elements; business operations stay with callers.
export default function ItemMenu({ children, label, disabled = false }) {
  const { t } = useI18n();
  return (
    <span className="item-menu" onPointerDown={(e) => e.stopPropagation()}>
      <Menu
        label={label || t('trash.menu')}
        disabled={disabled}
        trigger={
          <button type="button" onClick={(e) => e.stopPropagation()}>
            <MoreHorizontal size={16} aria-hidden="true" />
          </button>
        }
      >
        {children}
      </Menu>
    </span>
  );
}
