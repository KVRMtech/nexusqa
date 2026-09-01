/**
 * LOGIN SLOT FIELDS — one input per field the RECORDING watched the app ask for.
 *
 * Recording captures the steps and a session, never what you type (the observer is
 * value-free by construction). That is a deliberate security property, but on its own
 * it leaves a recording able to replay only a SESSION: a snapshot that expires, and
 * that an app whose login lives in client-side state can never restore — so "record
 * once" quietly meant "one crawl", and re-recording produced the same dead session.
 *
 * The recording already knows exactly WHICH fields the login needs. Asking for those
 * values here — deliberately entered, never sniffed — is what makes one recording sign
 * every future crawl in. Generic: the fields come from the observed recipe, so an
 * email+password login, a member#+PIN login and a 3-slot MFA login all render correctly
 * with no app-specific knowledge.
 */

/** A slot is either the recorded ``{name,type}`` or a bare name. */
export type LoginSlot = { name?: string; type?: string } | string;

export interface LoginSlotFieldsProps {
  /** The slots the recording observed (recipe `slots`, else `slot_names`). */
  slots: LoginSlot[];
  values: Record<string, string>;
  onChange: (values: Record<string, string>) => void;
  /** Tailwind classes for each input (each surface passes its own). */
  inputClassName?: string;
  disabled?: boolean;
}

const SECRET_HINTS = ['password', 'passcode', 'pin', 'secret'];

export function slotName(slot: LoginSlot): string {
  return (typeof slot === 'string' ? slot : slot?.name ?? '').trim();
}

/** Mask the input when the app masked it, or when the NAME says it is a secret (a PIN
 *  box is often a plain text input yet is still a secret to the person typing it). */
export function isSecretSlot(slot: LoginSlot): boolean {
  const name = slotName(slot).toLowerCase();
  const type = (typeof slot === 'string' ? '' : slot?.type ?? '').toLowerCase();
  return type === 'secret' || SECRET_HINTS.some((h) => name.includes(h));
}

export function LoginSlotFields({
  slots, values, onChange, inputClassName = '', disabled,
}: LoginSlotFieldsProps) {
  const names = slots.map(slotName).filter(Boolean);
  if (!names.length) return null;

  return (
    <div className="grid sm:grid-cols-2 gap-3">
      {slots.map((slot, i) => {
        const name = slotName(slot);
        if (!name) return null;
        const secret = isSecretSlot(slot);
        return (
          <label key={`${name}-${i}`} className="block">
            <span className="block text-2xs font-semibold text-ink-mid mb-1">{name}</span>
            <input
              className={inputClassName}
              type={secret ? 'password' : 'text'}
              autoComplete="off"
              disabled={disabled}
              value={values[name] ?? ''}
              onChange={(e) => onChange({ ...values, [name]: e.target.value })}
              aria-label={name}
            />
          </label>
        );
      })}
    </div>
  );
}

export default LoginSlotFields;
