/**
 * VkMark — the VKPower brand mark (identical to VKPower Video's /vkpower-icon.svg):
 * a navy squircle with a gold border, white "VK", and a gold power bolt bridging
 * the letters. Inline SVG so it scales crisply and needs no network request; the
 * same asset is also wired as the favicon in index.html.
 */
export interface VkMarkProps {
  size?: number;
  className?: string;
  title?: string;
}

export function VkMark({ size = 30, className, title = 'VKPower' }: VkMarkProps) {
  const decorative = title === '';
  return (
    <svg
      width={size}
      height={size}
      viewBox="0 0 256 256"
      className={className}
      role={decorative ? 'presentation' : 'img'}
      aria-hidden={decorative || undefined}
      aria-label={decorative ? undefined : title}
    >
      {!decorative && <title>{title}</title>}
      <defs>
        <linearGradient id="vk-bg" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#0d2f56" />
          <stop offset="1" stopColor="#071a33" />
        </linearGradient>
        <linearGradient id="vk-bolt" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0" stopColor="#ffd966" />
          <stop offset="1" stopColor="#e6a817" />
        </linearGradient>
      </defs>
      <rect x="8" y="8" width="240" height="240" rx="52" fill="url(#vk-bg)" />
      <rect x="8" y="8" width="240" height="240" rx="52" fill="none" stroke="#e6a817" strokeOpacity="0.35" strokeWidth="4" />
      {/* V */}
      <path d="M46 78 L78 78 L103 156 L96 178 Z" fill="#ffffff" />
      {/* K stem */}
      <rect x="150" y="78" width="26" height="100" rx="4" fill="#ffffff" />
      {/* K arms */}
      <path d="M176 124 L214 78 L236 78 L196 128 L236 178 L214 178 Z" fill="#ffffff" opacity="0.92" />
      {/* power bolt bridging V and K */}
      <path d="M141 62 L108 132 L131 132 L114 196 L162 118 L136 118 L156 62 Z" fill="url(#vk-bolt)" />
    </svg>
  );
}

export default VkMark;
