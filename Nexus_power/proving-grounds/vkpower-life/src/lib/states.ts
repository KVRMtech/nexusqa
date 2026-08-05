export const US_STATES = [
  { code: 'AL', name: 'Alabama' }, { code: 'AK', name: 'Alaska' },
  { code: 'AZ', name: 'Arizona' }, { code: 'AR', name: 'Arkansas' },
  { code: 'CA', name: 'California' }, { code: 'CO', name: 'Colorado' },
  { code: 'CT', name: 'Connecticut' }, { code: 'DE', name: 'Delaware' },
  { code: 'FL', name: 'Florida' }, { code: 'GA', name: 'Georgia' },
  { code: 'HI', name: 'Hawaii' }, { code: 'ID', name: 'Idaho' },
  { code: 'IL', name: 'Illinois' }, { code: 'IN', name: 'Indiana' },
  { code: 'IA', name: 'Iowa' }, { code: 'KS', name: 'Kansas' },
  { code: 'KY', name: 'Kentucky' }, { code: 'LA', name: 'Louisiana' },
  { code: 'ME', name: 'Maine' }, { code: 'MD', name: 'Maryland' },
  { code: 'MA', name: 'Massachusetts' }, { code: 'MI', name: 'Michigan' },
  { code: 'MN', name: 'Minnesota' }, { code: 'MS', name: 'Mississippi' },
  { code: 'MO', name: 'Missouri' }, { code: 'MT', name: 'Montana' },
  { code: 'NE', name: 'Nebraska' }, { code: 'NV', name: 'Nevada' },
  { code: 'NH', name: 'New Hampshire' }, { code: 'NJ', name: 'New Jersey' },
  { code: 'NM', name: 'New Mexico' }, { code: 'NY', name: 'New York' },
  { code: 'NC', name: 'North Carolina' }, { code: 'ND', name: 'North Dakota' },
  { code: 'OH', name: 'Ohio' }, { code: 'OK', name: 'Oklahoma' },
  { code: 'OR', name: 'Oregon' }, { code: 'PA', name: 'Pennsylvania' },
  { code: 'RI', name: 'Rhode Island' }, { code: 'SC', name: 'South Carolina' },
  { code: 'SD', name: 'South Dakota' }, { code: 'TN', name: 'Tennessee' },
  { code: 'TX', name: 'Texas' }, { code: 'UT', name: 'Utah' },
  { code: 'VT', name: 'Vermont' }, { code: 'VA', name: 'Virginia' },
  { code: 'WA', name: 'Washington' }, { code: 'WV', name: 'West Virginia' },
  { code: 'WI', name: 'Wisconsin' }, { code: 'WY', name: 'Wyoming' },
  { code: 'DC', name: 'District of Columbia' },
] as const;

export const COUNTRIES = [
  'United States', 'Canada', 'United Kingdom', 'Germany', 'France',
  'Australia', 'Japan', 'India', 'Mexico', 'Brazil', 'Other',
] as const;

export const RELATIONSHIPS = [
  'Spouse', 'Child', 'Parent', 'Sibling', 'Grandchild', 'Grandparent',
  'Trust', 'Estate', 'Charity', 'Business Partner', 'Other',
] as const;

export const MILITARY_BRANCHES = [
  'Army', 'Navy', 'Air Force', 'Marine Corps', 'Coast Guard', 'Space Force',
  'Army National Guard', 'Air National Guard', 'Army Reserve', 'Navy Reserve',
  'Air Force Reserve', 'Marine Corps Reserve', 'Coast Guard Reserve',
] as const;

export const MILITARY_STATUSES = [
  'Active Duty Officer', 'Active Duty Enlisted', 'Reserve/Guard',
  'Veteran (Honorable Discharge)', 'Veteran (Other Discharge)',
  'Retired Military', 'Military Spouse/Dependent', 'Civilian', 'None',
] as const;

export const COVERAGE_AMOUNTS = [
  { value: '50000', label: '$50,000' },
  { value: '100000', label: '$100,000' },
  { value: '150000', label: '$150,000' },
  { value: '200000', label: '$200,000' },
  { value: '250000', label: '$250,000' },
  { value: '300000', label: '$300,000' },
  { value: '400000', label: '$400,000' },
  { value: '500000', label: '$500,000' },
  { value: '750000', label: '$750,000' },
  { value: '1000000', label: '$1,000,000' },
  { value: '1500000', label: '$1,500,000' },
  { value: '2000000', label: '$2,000,000' },
] as const;

export const TERM_LENGTHS = [
  { value: '10', label: '10 Years' },
  { value: '15', label: '15 Years' },
  { value: '20', label: '20 Years' },
  { value: '25', label: '25 Years' },
  { value: '30', label: '30 Years' },
  { value: 'whole', label: 'Whole Life' },
] as const;

export const PRODUCTS = [
  { value: 'term-life', label: 'Term Life Insurance', description: 'Affordable coverage for a specific period' },
  { value: 'whole-life', label: 'Whole Life Insurance', description: 'Lifetime coverage with cash value accumulation' },
  { value: 'universal-life', label: 'Universal Life Insurance', description: 'Flexible premiums and adjustable death benefit' },
  { value: 'variable-life', label: 'Variable Universal Life', description: 'Investment options with life insurance protection' },
] as const;
