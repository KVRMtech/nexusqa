import { describe, it, expect, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { BrowserRouter } from 'react-router-dom';

// Mock AuthContext specifically for login
const mockLogin = vi.fn();
vi.mock('../../contexts/AuthContext', () => ({
  useAuth: () => ({
    user: null,
    isAuthenticated: false,
    loading: false,
    login: mockLogin,
    logout: vi.fn(),
    register: vi.fn(),
  }),
}));

import LoginPage from '../LoginPage';

function renderLogin() {
  return render(
    <BrowserRouter>
      <LoginPage />
    </BrowserRouter>,
  );
}

describe('LoginPage', () => {
  it('renders login form with branding', () => {
    renderLogin();
    expect(screen.getByText(/AI Engine Factory/i)).toBeInTheDocument();
    expect(screen.getByText(/Sign in to continue/i)).toBeInTheDocument();
    expect(screen.getByPlaceholderText('admin@company.com')).toBeInTheDocument();
    expect(screen.getByPlaceholderText('••••••••')).toBeInTheDocument();
  });

  it('has register link', () => {
    renderLogin();
    expect(screen.getByText(/Register here/i)).toBeInTheDocument();
  });

  it('shows engine status dots', () => {
    renderLogin();
    expect(screen.getByText(/11 Engines Ready/i)).toBeInTheDocument();
  });

  it('submits form and calls login', async () => {
    const user = userEvent.setup();
    renderLogin();
    await user.type(screen.getByPlaceholderText('admin@company.com'), 'test@test.com');
    await user.type(screen.getByPlaceholderText('••••••••'), 'password123');
    await user.click(screen.getByText('Sign in'));
    expect(mockLogin).toHaveBeenCalledWith('test@test.com', 'password123');
  }, 15000);
});
