import React, { useEffect, useState } from 'react';
import { Button } from './ui/Button.jsx';
import { Moon, Sun } from 'lucide-react';
import { useI18n } from './i18n/I18nProvider.jsx';
import { DARK, LIGHT, applySkin, isLightSkin, readSkin } from './skins.js';

// One click between the light and dark look (owner, 2026-10-26). Before the first click the
// app follows the system setting.
export default function ThemeToggle() {
  const { t } = useI18n();
  const [skin, setSkin] = useState(readSkin);
  useEffect(() => {
    const sync = (event) => setSkin(event.detail);
    window.addEventListener('inkseq:skin', sync);
    return () => window.removeEventListener('inkseq:skin', sync);
  }, []);
  const light = isLightSkin(skin);
  const label = t(light ? 'skin.toDark' : 'skin.toLight');
  return (
    <Button
      variant="ghost"
      className="theme-toggle"
      aria-label={label}
      title={label}
      onClick={() => applySkin(light ? DARK : LIGHT)}
    >
      {light ? <Moon size={15} /> : <Sun size={15} />}
    </Button>
  );
}
