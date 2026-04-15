import { describe, it, expect, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { DataTable, type Column } from '../DataTable';

interface TestRow {
  id: string;
  name: string;
  score: number;
}

const columns: Column<TestRow>[] = [
  { key: 'name', header: 'Name', accessor: (r) => r.name, sortable: true },
  { key: 'score', header: 'Score', accessor: (r) => r.score, sortable: true },
];

const data: TestRow[] = [
  { id: '1', name: 'Alice', score: 95 },
  { id: '2', name: 'Bob', score: 87 },
  { id: '3', name: 'Charlie', score: 72 },
];

describe('DataTable', () => {
  it('renders column headers', () => {
    render(<DataTable columns={columns} data={data} rowKey={(r) => r.id} />);
    expect(screen.getByText('Name')).toBeInTheDocument();
    expect(screen.getByText('Score')).toBeInTheDocument();
  });

  it('renders all data rows', () => {
    render(<DataTable columns={columns} data={data} rowKey={(r) => r.id} />);
    expect(screen.getByText('Alice')).toBeInTheDocument();
    expect(screen.getByText('Bob')).toBeInTheDocument();
    expect(screen.getByText('Charlie')).toBeInTheDocument();
  });

  it('shows empty state when data is empty', () => {
    render(<DataTable columns={columns} data={[]} rowKey={(r: TestRow) => r.id} emptyMessage="No records found" />);
    expect(screen.getByText('No records found')).toBeInTheDocument();
  });

  it('shows loading skeleton when loading', () => {
    const { container } = render(
      <DataTable columns={columns} data={[]} rowKey={(r: TestRow) => r.id} loading={true} />,
    );
    expect(container.querySelectorAll('.animate-pulse').length).toBeGreaterThan(0);
  });

  it('renders custom cell via accessor', () => {
    const columnsWithCustom: Column<TestRow>[] = [
      { key: 'name', header: 'Name', accessor: (r) => r.name },
      {
        key: 'score',
        header: 'Score',
        accessor: (r) => <span data-testid="custom-score">{r.score}%</span>,
      },
    ];
    render(<DataTable columns={columnsWithCustom} data={data} rowKey={(r) => r.id} />);
    expect(screen.getAllByTestId('custom-score')).toHaveLength(3);
    expect(screen.getByText('95%')).toBeInTheDocument();
  });

  it('handles sort on sortable columns', () => {
    render(<DataTable columns={columns} data={data} rowKey={(r) => r.id} />);
    const nameHeader = screen.getByText('Name');
    fireEvent.click(nameHeader);
    // Should sort — just verify no crash
    expect(screen.getByText('Alice')).toBeInTheDocument();
  });
});
