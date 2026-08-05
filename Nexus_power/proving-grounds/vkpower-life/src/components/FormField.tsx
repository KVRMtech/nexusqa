'use client';

import { type InputHTMLAttributes, type SelectHTMLAttributes, type TextareaHTMLAttributes } from 'react';

interface BaseProps {
  label: string;
  id: string;
  error?: string;
  required?: boolean;
  helpText?: string;
}

type InputProps = BaseProps & InputHTMLAttributes<HTMLInputElement> & { as?: 'input' };
type SelectProps = BaseProps & SelectHTMLAttributes<HTMLSelectElement> & { as: 'select'; options: { value: string; label: string }[] };
type TextareaProps = BaseProps & TextareaHTMLAttributes<HTMLTextAreaElement> & { as: 'textarea' };
type YesNoProps = BaseProps & { as: 'yesno'; value: string; onChange: (value: string) => void };

type Props = InputProps | SelectProps | TextareaProps | YesNoProps;

export default function FormField(props: Props) {
  const { label, id, error, required, helpText } = props;

  const labelEl = (
    <label htmlFor={id} className="form-label">
      {label}
      {required && <span className="text-red-500 ml-0.5">*</span>}
    </label>
  );

  if (props.as === 'yesno') {
    const { value, onChange } = props;
    return (
      <div>
        {labelEl}
        <div className="flex gap-3 mt-1">
          <button
            type="button"
            id={id}
            onClick={() => onChange('Yes')}
            className={`px-5 py-2 rounded-lg text-sm font-semibold border transition-colors ${
              value === 'Yes'
                ? 'bg-navy text-white border-navy'
                : 'bg-white text-gray-600 border-gray-300 hover:border-gray-400'
            }`}
          >
            Yes
          </button>
          <button
            type="button"
            onClick={() => onChange('No')}
            className={`px-5 py-2 rounded-lg text-sm font-semibold border transition-colors ${
              value === 'No'
                ? 'bg-navy text-white border-navy'
                : 'bg-white text-gray-600 border-gray-300 hover:border-gray-400'
            }`}
          >
            No
          </button>
        </div>
        {helpText && <p className="text-xs text-gray-500 mt-1">{helpText}</p>}
        {error && <p className="form-error">{error}</p>}
      </div>
    );
  }

  if (props.as === 'select') {
    const { options, as: _, ...selectProps } = props;
    return (
      <div>
        {labelEl}
        <select id={id} className="form-input" {...(selectProps as SelectHTMLAttributes<HTMLSelectElement>)}>
          <option value="">Select...</option>
          {options.map(o => (
            <option key={o.value} value={o.value}>{o.label}</option>
          ))}
        </select>
        {helpText && <p className="text-xs text-gray-500 mt-1">{helpText}</p>}
        {error && <p className="form-error">{error}</p>}
      </div>
    );
  }

  if (props.as === 'textarea') {
    const { as: _, ...textareaProps } = props;
    return (
      <div>
        {labelEl}
        <textarea
          id={id}
          className="form-input min-h-[80px] resize-y"
          {...(textareaProps as TextareaHTMLAttributes<HTMLTextAreaElement>)}
        />
        {helpText && <p className="text-xs text-gray-500 mt-1">{helpText}</p>}
        {error && <p className="form-error">{error}</p>}
      </div>
    );
  }

  const { as: _, id: _id, label: _l, error: _e, required: _r, helpText: _h, ...inputProps } = props as InputProps;
  return (
    <div>
      {labelEl}
      <input id={id} className="form-input" {...inputProps} />
      {helpText && <p className="text-xs text-gray-500 mt-1">{helpText}</p>}
      {error && <p className="form-error">{error}</p>}
    </div>
  );
}
