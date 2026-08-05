import { MemberProfile } from './types';

export const MEMBER_PROFILES: Record<string, MemberProfile> = {
  '25000001': {
    memberNumber: '25000001',
    firstName: 'Maria', lastName: 'Young',
    dateOfBirth: '2001-03-15', age: 25, gender: 'Female',
    ssn: '***-**-4521',
    email: 'maria.young@email.com', phone: '(210) 555-0142',
    address: { street: '4820 Crescent Park Dr', unit: 'Apt 302', city: 'San Antonio', state: 'TX', zip: '78229' },
    employment: { status: 'Active Duty', occupation: 'Intelligence Officer', employer: 'U.S. Army', annualIncome: '78000' },
    citizenship: 'United States', residencyState: 'TX',
    militaryStatus: 'Active Duty Officer', militaryBranch: 'Army',
    behaviorClass: 'young-officer',
    policyNumber: 'VKPL-25000001', coverageAmount: 250000, monthlyPremium: 18.40,
    beneficiaries: [
      { firstName: 'Liam', lastName: 'Young', relationship: 'Child', percentage: 50, dateOfBirth: '2023-06-10', ssn: '***-**-8812', type: 'primary' },
      { firstName: 'Ava', lastName: 'Young', relationship: 'Child', percentage: 50, dateOfBirth: '2025-01-22', ssn: '***-**-9934', type: 'primary' },
    ],
  },
  '25000002': {
    memberNumber: '25000002',
    firstName: 'James', lastName: 'Torres',
    dateOfBirth: '1996-08-22', age: 29, gender: 'Male',
    ssn: '***-**-6738',
    email: 'james.torres@email.com', phone: '(757) 555-0387',
    address: { street: '1105 Oceanview Blvd', unit: '', city: 'Norfolk', state: 'VA', zip: '23505' },
    employment: { status: 'Active Duty', occupation: 'Petty Officer Second Class', employer: 'U.S. Navy', annualIncome: '52000' },
    citizenship: 'United States', residencyState: 'VA',
    militaryStatus: 'Active Duty Enlisted', militaryBranch: 'Navy',
    behaviorClass: 'enlisted-single',
    policyNumber: 'VKPL-25000002', coverageAmount: 150000, monthlyPremium: 14.20,
    beneficiaries: [
      { firstName: 'Rosa', lastName: 'Torres', relationship: 'Parent', percentage: 100, dateOfBirth: '1968-11-05', ssn: '***-**-2201', type: 'primary' },
    ],
  },
  '50000001': {
    memberNumber: '50000001',
    firstName: 'Robert', lastName: 'Mitchell',
    dateOfBirth: '1978-05-10', age: 48, gender: 'Male',
    ssn: '***-**-3356',
    email: 'robert.mitchell@email.com', phone: '(719) 555-0921',
    address: { street: '8432 Mountain Shadow Ln', unit: '', city: 'Colorado Springs', state: 'CO', zip: '80920' },
    employment: { status: 'Employed', occupation: 'Defense Contractor', employer: 'Lockheed Martin', annualIncome: '125000' },
    citizenship: 'United States', residencyState: 'CO',
    militaryStatus: 'Veteran (Honorable Discharge)', militaryBranch: 'Marine Corps',
    behaviorClass: 'veteran-mid-career',
    policyNumber: 'VKPL-50000001', coverageAmount: 500000, monthlyPremium: 72.50,
    beneficiaries: [
      { firstName: 'Sandra', lastName: 'Mitchell', relationship: 'Spouse', percentage: 60, dateOfBirth: '1980-09-17', ssn: '***-**-4478', type: 'primary' },
      { firstName: 'Tyler', lastName: 'Mitchell', relationship: 'Child', percentage: 20, dateOfBirth: '2006-02-28', ssn: '***-**-5590', type: 'primary' },
      { firstName: 'Olivia', lastName: 'Mitchell', relationship: 'Child', percentage: 20, dateOfBirth: '2009-07-14', ssn: '***-**-6612', type: 'primary' },
    ],
  },
  '50000005': {
    memberNumber: '50000005',
    firstName: 'John', lastName: 'Senior',
    dateOfBirth: '1971-11-28', age: 55, gender: 'Male',
    ssn: '***-**-1189',
    email: 'john.senior@email.com', phone: '(904) 555-0654',
    address: { street: '2210 Riverside Ave', unit: 'Unit 15B', city: 'Jacksonville', state: 'FL', zip: '32204' },
    employment: { status: 'Employed', occupation: 'Financial Advisor', employer: 'Edward Jones', annualIncome: '142000' },
    citizenship: 'United States', residencyState: 'FL',
    militaryStatus: 'Civilian', militaryBranch: '',
    behaviorClass: 'civilian-senior',
    policyNumber: 'VKPL-50000005', coverageAmount: 500000, monthlyPremium: 96.70,
    beneficiaries: [
      { firstName: 'Emma', lastName: 'Senior', relationship: 'Child', percentage: 20, dateOfBirth: '1998-04-12', ssn: '***-**-7745', type: 'primary' },
      { firstName: 'Noah', lastName: 'Senior', relationship: 'Child', percentage: 20, dateOfBirth: '2000-08-30', ssn: '***-**-8856', type: 'primary' },
      { firstName: 'Olivia', lastName: 'Senior', relationship: 'Child', percentage: 20, dateOfBirth: '2003-01-06', ssn: '***-**-9967', type: 'primary' },
      { firstName: 'Mia', lastName: 'Senior', relationship: 'Child', percentage: 20, dateOfBirth: '2005-12-19', ssn: '***-**-1078', type: 'primary' },
      { firstName: 'Lucas', lastName: 'Senior', relationship: 'Child', percentage: 20, dateOfBirth: '2008-06-25', ssn: '***-**-2189', type: 'primary' },
    ],
  },
  '75000001': {
    memberNumber: '75000001',
    firstName: 'Patricia', lastName: 'Warren',
    dateOfBirth: '1965-02-14', age: 61, gender: 'Female',
    ssn: '***-**-5523',
    email: 'patricia.warren@email.com', phone: '(602) 555-0812',
    address: { street: '5601 N Central Ave', unit: 'Suite 200', city: 'Phoenix', state: 'AZ', zip: '85012' },
    employment: { status: 'Retired', occupation: 'Retired Nurse', employer: 'N/A', annualIncome: '68000' },
    citizenship: 'United States', residencyState: 'AZ',
    militaryStatus: 'Retired Military', militaryBranch: 'Air Force',
    behaviorClass: 'retired-complex',
    policyNumber: 'VKPL-75000001', coverageAmount: 300000, monthlyPremium: 142.30,
    beneficiaries: [
      { firstName: 'David', lastName: 'Warren', relationship: 'Spouse', percentage: 50, dateOfBirth: '1963-07-20', ssn: '***-**-6634', type: 'primary' },
      { firstName: 'Jennifer', lastName: 'Warren-Cole', relationship: 'Child', percentage: 25, dateOfBirth: '1990-10-05', ssn: '***-**-7745', type: 'primary' },
      { firstName: 'Michael', lastName: 'Warren', relationship: 'Child', percentage: 25, dateOfBirth: '1993-03-18', ssn: '***-**-8856', type: 'primary' },
    ],
  },
};

export function lookupMember(memberNumber: string): MemberProfile | null {
  return MEMBER_PROFILES[memberNumber] ?? null;
}

export function getDefaultProfile(memberNumber: string): MemberProfile {
  return {
    memberNumber,
    firstName: 'VKPower', lastName: 'Member',
    dateOfBirth: '1985-06-15', age: 41, gender: 'Male',
    ssn: '***-**-0000',
    email: 'member@vkpower.com', phone: '(800) 555-0100',
    address: { street: '100 Main Street', unit: '', city: 'Dallas', state: 'TX', zip: '75201' },
    employment: { status: 'Employed', occupation: 'Professional', employer: 'Employer', annualIncome: '75000' },
    citizenship: 'United States', residencyState: 'TX',
    militaryStatus: 'None', militaryBranch: '',
    behaviorClass: 'generic',
    policyNumber: `VKPL-${memberNumber}`, coverageAmount: 200000, monthlyPremium: 32.00,
    beneficiaries: [
      { firstName: 'Primary', lastName: 'Beneficiary', relationship: 'Spouse', percentage: 100, dateOfBirth: '1987-04-20', ssn: '***-**-0001', type: 'primary' },
    ],
  };
}
