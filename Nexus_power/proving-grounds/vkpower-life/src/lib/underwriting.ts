export interface UnderwritingResult {
  decision: 'approved' | 'referred' | 'declined';
  riskClass: string;
  reasons: string[];
  premiumMultiplier: number;
}

export function evaluateUnderwriting(
  age: number,
  gender: string,
  bmi: number,
  tobaccoUse: boolean,
  healthAnswers: Record<string, string>,
  lifestyleAnswers: Record<string, string>,
  militaryStatus: string,
  coverageAmount: number,
): UnderwritingResult {
  const reasons: string[] = [];
  let score = 0;

  if (age > 65) { score += 30; reasons.push('Age exceeds 65 — elevated mortality risk'); }
  else if (age > 55) { score += 15; reasons.push('Age between 56 and 65 — moderate risk factor'); }
  else if (age > 45) { score += 5; }

  if (bmi > 40) { score += 40; reasons.push('BMI exceeds 40 — Class III obesity'); }
  else if (bmi > 35) { score += 25; reasons.push('BMI exceeds 35 — Class II obesity'); }
  else if (bmi > 30) { score += 10; reasons.push('BMI exceeds 30 — Class I obesity'); }
  else if (bmi < 16) { score += 25; reasons.push('BMI below 16 — severe underweight'); }

  if (tobaccoUse) { score += 20; reasons.push('Current tobacco use — smoker rates apply'); }

  const critical = ['cv_3', 'cv_7', 'can_1', 'neuro_4', 'neuro_5', 'blood_3'];
  const major = ['cv_4', 'cv_5', 'cv_8', 'resp_2', 'resp_5', 'endo_1', 'gi_1', 'mh_5', 'mh_6', 'gu_1'];
  const moderate = ['cv_1', 'cv_2', 'cv_6', 'cv_9', 'resp_1', 'resp_3', 'neuro_1', 'neuro_2', 'mh_1', 'mh_2', 'mh_3', 'mh_4', 'endo_2', 'endo_3', 'gi_2', 'gi_3', 'ms_1', 'blood_2', 'blood_4'];

  for (const id of critical) {
    if (healthAnswers[id] === 'Yes') {
      score += 50;
      const q = id.replace(/_\d+$/, '');
      reasons.push(`Critical condition reported: ${formatConditionId(id)}`);
    }
  }
  for (const id of major) {
    if (healthAnswers[id] === 'Yes') {
      score += 20;
      reasons.push(`Major condition reported: ${formatConditionId(id)}`);
    }
  }
  for (const id of moderate) {
    if (healthAnswers[id] === 'Yes') { score += 8; }
  }

  if (healthAnswers['can_1'] === 'Yes') {
    const stage = healthAnswers['can_1_stage'] || '';
    if (stage.includes('III') || stage.includes('IV')) { score += 40; reasons.push('Late-stage cancer diagnosis'); }
    if (healthAnswers['can_1_remission'] !== 'Yes') { score += 30; reasons.push('Cancer not in remission'); }
  }

  if (healthAnswers['endo_1'] === 'Yes' || healthAnswers['endo_2'] === 'Yes') {
    const a1c = parseFloat(healthAnswers['endo_1_a1c'] || healthAnswers['endo_2_a1c'] || '0');
    if (a1c > 9) { score += 25; reasons.push(`HbA1c level ${a1c}% — poorly controlled diabetes`); }
    else if (a1c > 7.5) { score += 10; reasons.push(`HbA1c level ${a1c}% — above target range`); }
    if (healthAnswers['endo_1_complications'] === 'Yes') { score += 20; reasons.push('Diabetes complications present'); }
  }

  if (healthAnswers['mh_6'] === 'Yes') {
    const when = healthAnswers['mh_6_when'] || '';
    if (when.includes('12 months')) { score += 50; reasons.push('Suicide attempt or ideation within past 12 months'); }
    else if (when.includes('1-3')) { score += 30; reasons.push('Suicide attempt or ideation within past 3 years'); }
  }

  if (healthAnswers['gu_1_dialysis'] === 'Yes') { score += 40; reasons.push('Currently on dialysis'); }
  if (healthAnswers['resp_2_oxygen'] === 'Yes') { score += 30; reasons.push('Requires supplemental oxygen'); }

  const ls = lifestyleAnswers;
  if (ls['ls_tobacco'] === 'Yes') { score += 15; reasons.push('Active tobacco use confirmed in lifestyle section'); }
  if (ls['ls_marijuana'] === 'Yes') { score += 5; }
  if (ls['ls_drugs'] === 'Yes') { score += 30; reasons.push('Recreational drug use reported'); }
  if (ls['ls_dui'] === 'Yes') {
    const count = parseInt(ls['ls_dui_count'] || '0');
    if (count >= 2) { score += 25; reasons.push(`Multiple DUI/DWI convictions (${count})`); }
    else { score += 10; reasons.push('DUI/DWI conviction on record'); }
  }
  if (ls['ls_felony'] === 'Yes') { score += 30; reasons.push('Felony conviction on record'); }
  if (ls['ls_aviation'] === 'Yes' && ls['ls_aviation_type'] === 'Private Pilot') { score += 10; }
  if (ls['ls_skydiving'] === 'Yes' || ls['ls_scuba_depth'] === 'Yes') { score += 8; }
  if (ls['ls_combat'] === 'Yes') { score += 15; reasons.push('Active combat zone deployment'); }

  if (coverageAmount > 1000000) { score += 5; reasons.push('High face amount — financial underwriting review'); }

  if (militaryStatus === 'Active Duty Officer' || militaryStatus === 'Active Duty Enlisted') {
    if (ls['ls_combat'] !== 'Yes') { score = Math.max(0, score - 5); }
  }

  let decision: 'approved' | 'referred' | 'declined';
  let riskClass: string;
  let premiumMultiplier: number;

  if (score >= 80) {
    decision = 'declined';
    riskClass = 'Declined';
    premiumMultiplier = 0;
    if (reasons.length === 0) reasons.push('Combined risk factors exceed acceptable threshold');
  } else if (score >= 40) {
    decision = 'referred';
    riskClass = 'Refer to Underwriter';
    premiumMultiplier = 1.5 + (score - 40) * 0.02;
    if (reasons.length === 0) reasons.push('Additional underwriting review required');
  } else if (score >= 20) {
    decision = 'approved';
    riskClass = tobaccoUse ? 'Standard Tobacco' : 'Standard';
    premiumMultiplier = 1.0 + score * 0.015;
  } else if (score >= 10) {
    decision = 'approved';
    riskClass = tobaccoUse ? 'Preferred Tobacco' : 'Preferred';
    premiumMultiplier = tobaccoUse ? 1.3 : 0.85;
  } else {
    decision = 'approved';
    riskClass = 'Preferred Plus';
    premiumMultiplier = 0.7;
  }

  return { decision, riskClass, reasons, premiumMultiplier };
}

export function calculatePremium(
  coverageAmount: number,
  termLength: string,
  age: number,
  gender: string,
  multiplier: number,
): number {
  const basePer1000 = 0.08;
  const ageFactor = 1 + Math.pow((age - 25) / 40, 1.8) * 2.5;
  const termFactor = termLength === 'whole' ? 6.0 : 1 + (parseInt(termLength) - 10) * 0.04;
  const genderFactor = gender === 'Female' ? 0.88 : 1.0;
  const monthly = (coverageAmount / 1000) * basePer1000 * ageFactor * termFactor * genderFactor * multiplier;
  return Math.round(monthly * 100) / 100;
}

function formatConditionId(id: string): string {
  const map: Record<string, string> = {
    cv_3: 'Heart attack', cv_4: 'Coronary artery disease', cv_5: 'Cardiac surgery',
    cv_7: 'Congestive heart failure', cv_8: 'Stroke or TIA',
    can_1: 'Cancer', neuro_4: 'ALS', neuro_5: "Alzheimer's/Dementia",
    blood_3: 'HIV/AIDS', resp_2: 'COPD/Emphysema', resp_5: 'Pulmonary fibrosis',
    endo_1: 'Type 1 diabetes', gi_1: 'Liver disease', mh_5: 'Schizophrenia',
    mh_6: 'Suicide attempt/ideation', gu_1: 'Kidney disease',
    cv_1: 'Hypertension', cv_6: 'Arrhythmia', cv_9: 'Vascular disease',
    resp_1: 'Asthma', resp_3: 'Sleep apnea', neuro_1: 'Epilepsy', neuro_2: 'Multiple sclerosis',
    mh_1: 'Depression', mh_3: 'Bipolar disorder', mh_4: 'PTSD',
    endo_2: 'Type 2 diabetes', endo_3: 'Thyroid disorder', gi_2: "Crohn's/Colitis",
    gi_3: 'Pancreatitis', ms_1: 'Chronic back/neck condition', blood_2: 'Clotting disorder', blood_4: 'Lupus/Autoimmune',
  };
  return map[id] || id;
}
