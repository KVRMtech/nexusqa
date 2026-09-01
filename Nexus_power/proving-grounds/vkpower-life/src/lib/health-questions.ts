import { HealthQuestion } from './types';

function yesFollowUps(parentId: string, category: string): HealthQuestion[] {
  return [
    { id: `${parentId}_date`, text: 'Date of diagnosis or occurrence', category, type: 'date', dependsOn: { questionId: parentId, answer: 'Yes' } },
    { id: `${parentId}_treatment`, text: 'Describe current treatment or medication', category, type: 'textarea', dependsOn: { questionId: parentId, answer: 'Yes' }, placeholder: 'Include medication names, dosages, and treating physician' },
    { id: `${parentId}_status`, text: 'Current status of this condition', category, type: 'select', options: ['Active - Under Treatment', 'Controlled with Medication', 'Resolved - No Treatment', 'Monitoring Only', 'Awaiting Treatment'], dependsOn: { questionId: parentId, answer: 'Yes' } },
  ];
}

const cat = (c: string) => c;

const GENERAL = cat('General Health');
const CARDIO = cat('Cardiovascular');
const RESPIRATORY = cat('Respiratory');
const CANCER = cat('Cancer & Neoplasm');
const NEURO = cat('Neurological');
const MENTAL = cat('Mental Health');
const ENDOCRINE = cat('Endocrine & Metabolic');
const GI = cat('Gastrointestinal');
const MUSCULO = cat('Musculoskeletal');
const GU = cat('Genitourinary');
const BLOOD = cat('Blood & Immune');
const SENSORY = cat('Eyes, Ears & Skin');
const FAMILY = cat('Family Medical History');
const MEDS = cat('Medications & Hospitalizations');

export const HEALTH_QUESTIONS: HealthQuestion[] = [
  // ─── GENERAL HEALTH ───────────────────────────────────────────────────────
  { id: 'gen_1', text: 'What is your height (feet)?', category: GENERAL, type: 'select', options: ['4', '5', '6', '7'] },
  { id: 'gen_2', text: 'What is your height (inches)?', category: GENERAL, type: 'select', options: ['0','1','2','3','4','5','6','7','8','9','10','11'] },
  { id: 'gen_3', text: 'What is your current weight in pounds?', category: GENERAL, type: 'number', placeholder: 'e.g. 170' },
  { id: 'gen_4', text: 'Have you gained or lost more than 10 pounds in the past 12 months without intentional diet or exercise?', category: GENERAL, type: 'yesno' },
  { id: 'gen_4_amount', text: 'How many pounds were gained or lost?', category: GENERAL, type: 'number', dependsOn: { questionId: 'gen_4', answer: 'Yes' }, placeholder: 'e.g. 15' },
  { id: 'gen_4_reason', text: 'What was the reason for the weight change?', category: GENERAL, type: 'textarea', dependsOn: { questionId: 'gen_4', answer: 'Yes' } },
  { id: 'gen_5', text: 'Have you consulted a physician, been examined, or been treated in the past 5 years?', category: GENERAL, type: 'yesno' },
  ...yesFollowUps('gen_5', GENERAL),
  { id: 'gen_6', text: 'Have you ever been declined, postponed, or rated for life, health, or disability insurance?', category: GENERAL, type: 'yesno' },
  { id: 'gen_6_detail', text: 'Provide details including the company name and reason', category: GENERAL, type: 'textarea', dependsOn: { questionId: 'gen_6', answer: 'Yes' } },
  { id: 'gen_7', text: 'Are you currently disabled or receiving disability benefits?', category: GENERAL, type: 'yesno' },
  ...yesFollowUps('gen_7', GENERAL),

  // ─── CARDIOVASCULAR ────────────────────────────────────────────────────────
  { id: 'cv_1', text: 'Have you ever been diagnosed with or treated for high blood pressure (hypertension)?', category: CARDIO, type: 'yesno' },
  ...yesFollowUps('cv_1', CARDIO),
  { id: 'cv_1_reading', text: 'Most recent blood pressure reading', category: CARDIO, type: 'text', dependsOn: { questionId: 'cv_1', answer: 'Yes' }, placeholder: 'e.g. 130/85' },
  { id: 'cv_2', text: 'Have you ever been diagnosed with or treated for high cholesterol or elevated lipids?', category: CARDIO, type: 'yesno' },
  ...yesFollowUps('cv_2', CARDIO),
  { id: 'cv_2_level', text: 'Most recent total cholesterol level', category: CARDIO, type: 'number', dependsOn: { questionId: 'cv_2', answer: 'Yes' }, placeholder: 'e.g. 220' },
  { id: 'cv_3', text: 'Have you ever had a heart attack (myocardial infarction)?', category: CARDIO, type: 'yesno' },
  ...yesFollowUps('cv_3', CARDIO),
  { id: 'cv_4', text: 'Have you ever been diagnosed with coronary artery disease or angina?', category: CARDIO, type: 'yesno' },
  ...yesFollowUps('cv_4', CARDIO),
  { id: 'cv_5', text: 'Have you ever had heart surgery, angioplasty, stent placement, or cardiac catheterization?', category: CARDIO, type: 'yesno' },
  { id: 'cv_5_type', text: 'Type of cardiac procedure', category: CARDIO, type: 'select', options: ['Bypass Surgery (CABG)', 'Angioplasty', 'Stent Placement', 'Valve Replacement', 'Catheterization', 'Other'], dependsOn: { questionId: 'cv_5', answer: 'Yes' } },
  { id: 'cv_5_date', text: 'Date of cardiac procedure', category: CARDIO, type: 'date', dependsOn: { questionId: 'cv_5', answer: 'Yes' } },
  { id: 'cv_6', text: 'Have you ever been diagnosed with an irregular heartbeat (arrhythmia), atrial fibrillation, or heart murmur?', category: CARDIO, type: 'yesno' },
  ...yesFollowUps('cv_6', CARDIO),
  { id: 'cv_7', text: 'Have you ever been diagnosed with congestive heart failure or cardiomyopathy?', category: CARDIO, type: 'yesno' },
  ...yesFollowUps('cv_7', CARDIO),
  { id: 'cv_8', text: 'Have you ever had a stroke, transient ischemic attack (TIA), or carotid artery disease?', category: CARDIO, type: 'yesno' },
  ...yesFollowUps('cv_8', CARDIO),
  { id: 'cv_9', text: 'Have you ever been diagnosed with peripheral vascular disease or blood clots (DVT/PE)?', category: CARDIO, type: 'yesno' },
  ...yesFollowUps('cv_9', CARDIO),
  { id: 'cv_10', text: 'Do you have a pacemaker or implantable cardioverter-defibrillator (ICD)?', category: CARDIO, type: 'yesno' },
  { id: 'cv_10_date', text: 'Date the device was implanted', category: CARDIO, type: 'date', dependsOn: { questionId: 'cv_10', answer: 'Yes' } },

  // ─── RESPIRATORY ───────────────────────────────────────────────────────────
  { id: 'resp_1', text: 'Have you ever been diagnosed with or treated for asthma?', category: RESPIRATORY, type: 'yesno' },
  ...yesFollowUps('resp_1', RESPIRATORY),
  { id: 'resp_1_hosp', text: 'Have you been hospitalized for asthma in the past 5 years?', category: RESPIRATORY, type: 'yesno', dependsOn: { questionId: 'resp_1', answer: 'Yes' } },
  { id: 'resp_2', text: 'Have you ever been diagnosed with chronic obstructive pulmonary disease (COPD) or emphysema?', category: RESPIRATORY, type: 'yesno' },
  ...yesFollowUps('resp_2', RESPIRATORY),
  { id: 'resp_2_oxygen', text: 'Do you use supplemental oxygen?', category: RESPIRATORY, type: 'yesno', dependsOn: { questionId: 'resp_2', answer: 'Yes' } },
  { id: 'resp_3', text: 'Have you ever been diagnosed with sleep apnea?', category: RESPIRATORY, type: 'yesno' },
  { id: 'resp_3_cpap', text: 'Do you use a CPAP or BiPAP machine?', category: RESPIRATORY, type: 'yesno', dependsOn: { questionId: 'resp_3', answer: 'Yes' } },
  { id: 'resp_3_compliance', text: 'Are you compliant with CPAP therapy (use at least 4 hours per night)?', category: RESPIRATORY, type: 'yesno', dependsOn: { questionId: 'resp_3_cpap', answer: 'Yes' } },
  { id: 'resp_4', text: 'Have you ever had pneumonia, bronchitis, or a lung infection more than twice in the past 5 years?', category: RESPIRATORY, type: 'yesno' },
  ...yesFollowUps('resp_4', RESPIRATORY),
  { id: 'resp_5', text: 'Have you ever been diagnosed with pulmonary fibrosis, sarcoidosis, or interstitial lung disease?', category: RESPIRATORY, type: 'yesno' },
  ...yesFollowUps('resp_5', RESPIRATORY),
  { id: 'resp_6', text: 'Have you ever had a pulmonary embolism (blood clot in the lung)?', category: RESPIRATORY, type: 'yesno' },
  ...yesFollowUps('resp_6', RESPIRATORY),

  // ─── CANCER & NEOPLASM ─────────────────────────────────────────────────────
  { id: 'can_1', text: 'Have you ever been diagnosed with or treated for any form of cancer, tumor, or malignancy?', category: CANCER, type: 'yesno' },
  { id: 'can_1_type', text: 'Type of cancer diagnosed', category: CANCER, type: 'select', options: ['Skin (Non-Melanoma)', 'Skin (Melanoma)', 'Breast', 'Prostate', 'Lung', 'Colon/Rectal', 'Thyroid', 'Kidney', 'Bladder', 'Lymphoma', 'Leukemia', 'Brain', 'Pancreatic', 'Liver', 'Ovarian', 'Uterine/Cervical', 'Testicular', 'Other'], dependsOn: { questionId: 'can_1', answer: 'Yes' } },
  { id: 'can_1_date', text: 'Date of cancer diagnosis', category: CANCER, type: 'date', dependsOn: { questionId: 'can_1', answer: 'Yes' } },
  { id: 'can_1_stage', text: 'Stage at diagnosis', category: CANCER, type: 'select', options: ['Stage 0 (In Situ)', 'Stage I', 'Stage II', 'Stage III', 'Stage IV', 'Unknown'], dependsOn: { questionId: 'can_1', answer: 'Yes' } },
  { id: 'can_1_treatment', text: 'Treatment received', category: CANCER, type: 'select', options: ['Surgery Only', 'Radiation Only', 'Chemotherapy Only', 'Surgery + Radiation', 'Surgery + Chemotherapy', 'Surgery + Radiation + Chemo', 'Immunotherapy', 'Hormone Therapy', 'Watchful Waiting', 'Other Combination'], dependsOn: { questionId: 'can_1', answer: 'Yes' } },
  { id: 'can_1_remission', text: 'Are you currently in remission?', category: CANCER, type: 'yesno', dependsOn: { questionId: 'can_1', answer: 'Yes' } },
  { id: 'can_1_remission_date', text: 'Date remission was declared', category: CANCER, type: 'date', dependsOn: { questionId: 'can_1_remission', answer: 'Yes' } },
  { id: 'can_2', text: 'Have you ever had an abnormal biopsy, Pap smear, mammogram, or PSA test?', category: CANCER, type: 'yesno' },
  { id: 'can_2_type', text: 'Type of abnormal result', category: CANCER, type: 'text', dependsOn: { questionId: 'can_2', answer: 'Yes' }, placeholder: 'e.g. Abnormal Pap smear - ASCUS' },
  { id: 'can_2_followup', text: 'What follow-up was recommended or performed?', category: CANCER, type: 'textarea', dependsOn: { questionId: 'can_2', answer: 'Yes' } },
  { id: 'can_3', text: 'Have you ever been diagnosed with any pre-cancerous condition (polyps, dysplasia, DCIS, MGUS)?', category: CANCER, type: 'yesno' },
  ...yesFollowUps('can_3', CANCER),

  // ─── NEUROLOGICAL ──────────────────────────────────────────────────────────
  { id: 'neuro_1', text: 'Have you ever been diagnosed with or treated for seizures or epilepsy?', category: NEURO, type: 'yesno' },
  ...yesFollowUps('neuro_1', NEURO),
  { id: 'neuro_1_freq', text: 'How many seizures in the past 12 months?', category: NEURO, type: 'number', dependsOn: { questionId: 'neuro_1', answer: 'Yes' } },
  { id: 'neuro_2', text: 'Have you ever been diagnosed with multiple sclerosis (MS)?', category: NEURO, type: 'yesno' },
  ...yesFollowUps('neuro_2', NEURO),
  { id: 'neuro_3', text: 'Have you ever been diagnosed with Parkinson\'s disease or any movement disorder?', category: NEURO, type: 'yesno' },
  ...yesFollowUps('neuro_3', NEURO),
  { id: 'neuro_4', text: 'Have you ever been diagnosed with ALS (Lou Gehrig\'s disease) or muscular dystrophy?', category: NEURO, type: 'yesno' },
  ...yesFollowUps('neuro_4', NEURO),
  { id: 'neuro_5', text: 'Have you ever been diagnosed with Alzheimer\'s disease or any form of dementia?', category: NEURO, type: 'yesno' },
  ...yesFollowUps('neuro_5', NEURO),
  { id: 'neuro_6', text: 'Have you experienced chronic or recurring migraines or severe headaches requiring medical treatment?', category: NEURO, type: 'yesno' },
  ...yesFollowUps('neuro_6', NEURO),
  { id: 'neuro_7', text: 'Have you ever been diagnosed with neuropathy (nerve damage) or numbness/tingling in extremities?', category: NEURO, type: 'yesno' },
  ...yesFollowUps('neuro_7', NEURO),
  { id: 'neuro_8', text: 'Have you ever had a traumatic brain injury (TBI) or concussion requiring medical attention?', category: NEURO, type: 'yesno' },
  { id: 'neuro_8_count', text: 'How many concussions or TBIs have you had?', category: NEURO, type: 'number', dependsOn: { questionId: 'neuro_8', answer: 'Yes' } },
  { id: 'neuro_8_lasting', text: 'Do you have any lasting symptoms from the injury?', category: NEURO, type: 'yesno', dependsOn: { questionId: 'neuro_8', answer: 'Yes' } },

  // ─── MENTAL HEALTH ─────────────────────────────────────────────────────────
  { id: 'mh_1', text: 'Have you ever been diagnosed with or treated for depression?', category: MENTAL, type: 'yesno' },
  ...yesFollowUps('mh_1', MENTAL),
  { id: 'mh_1_hospital', text: 'Have you ever been hospitalized for depression?', category: MENTAL, type: 'yesno', dependsOn: { questionId: 'mh_1', answer: 'Yes' } },
  { id: 'mh_2', text: 'Have you ever been diagnosed with or treated for anxiety disorder, panic disorder, or OCD?', category: MENTAL, type: 'yesno' },
  ...yesFollowUps('mh_2', MENTAL),
  { id: 'mh_3', text: 'Have you ever been diagnosed with bipolar disorder or manic episodes?', category: MENTAL, type: 'yesno' },
  ...yesFollowUps('mh_3', MENTAL),
  { id: 'mh_4', text: 'Have you ever been diagnosed with post-traumatic stress disorder (PTSD)?', category: MENTAL, type: 'yesno' },
  ...yesFollowUps('mh_4', MENTAL),
  { id: 'mh_4_cause', text: 'Cause of PTSD', category: MENTAL, type: 'select', options: ['Combat/Military Service', 'Accident/Injury', 'Assault/Violence', 'Natural Disaster', 'Other Trauma'], dependsOn: { questionId: 'mh_4', answer: 'Yes' } },
  { id: 'mh_5', text: 'Have you ever been diagnosed with schizophrenia or a psychotic disorder?', category: MENTAL, type: 'yesno' },
  ...yesFollowUps('mh_5', MENTAL),
  { id: 'mh_6', text: 'Have you ever attempted suicide or had suicidal ideation?', category: MENTAL, type: 'yesno' },
  { id: 'mh_6_when', text: 'When was the most recent episode?', category: MENTAL, type: 'select', options: ['Within past 12 months', '1-3 years ago', '3-5 years ago', 'More than 5 years ago'], dependsOn: { questionId: 'mh_6', answer: 'Yes' } },
  { id: 'mh_6_treatment', text: 'Are you currently under professional care for this?', category: MENTAL, type: 'yesno', dependsOn: { questionId: 'mh_6', answer: 'Yes' } },
  { id: 'mh_7', text: 'Have you ever been treated for or diagnosed with an eating disorder (anorexia, bulimia)?', category: MENTAL, type: 'yesno' },
  ...yesFollowUps('mh_7', MENTAL),

  // ─── ENDOCRINE & METABOLIC ─────────────────────────────────────────────────
  { id: 'endo_1', text: 'Have you ever been diagnosed with Type 1 diabetes (insulin-dependent)?', category: ENDOCRINE, type: 'yesno' },
  ...yesFollowUps('endo_1', ENDOCRINE),
  { id: 'endo_1_a1c', text: 'Most recent HbA1c level', category: ENDOCRINE, type: 'number', dependsOn: { questionId: 'endo_1', answer: 'Yes' }, placeholder: 'e.g. 7.2' },
  { id: 'endo_1_complications', text: 'Any diabetes-related complications (retinopathy, neuropathy, nephropathy)?', category: ENDOCRINE, type: 'yesno', dependsOn: { questionId: 'endo_1', answer: 'Yes' } },
  { id: 'endo_2', text: 'Have you ever been diagnosed with Type 2 diabetes?', category: ENDOCRINE, type: 'yesno' },
  ...yesFollowUps('endo_2', ENDOCRINE),
  { id: 'endo_2_a1c', text: 'Most recent HbA1c level', category: ENDOCRINE, type: 'number', dependsOn: { questionId: 'endo_2', answer: 'Yes' }, placeholder: 'e.g. 6.8' },
  { id: 'endo_2_insulin', text: 'Are you currently using insulin?', category: ENDOCRINE, type: 'yesno', dependsOn: { questionId: 'endo_2', answer: 'Yes' } },
  { id: 'endo_3', text: 'Have you ever been diagnosed with a thyroid disorder (hypothyroidism, hyperthyroidism, Graves\' disease)?', category: ENDOCRINE, type: 'yesno' },
  ...yesFollowUps('endo_3', ENDOCRINE),
  { id: 'endo_4', text: 'Have you ever been diagnosed with an adrenal gland disorder (Addison\'s disease, Cushing\'s syndrome)?', category: ENDOCRINE, type: 'yesno' },
  ...yesFollowUps('endo_4', ENDOCRINE),
  { id: 'endo_5', text: 'Have you ever been diagnosed with metabolic syndrome or pre-diabetes?', category: ENDOCRINE, type: 'yesno' },
  ...yesFollowUps('endo_5', ENDOCRINE),
  { id: 'endo_6', text: 'Have you ever been diagnosed with gout?', category: ENDOCRINE, type: 'yesno' },
  ...yesFollowUps('endo_6', ENDOCRINE),

  // ─── GASTROINTESTINAL ──────────────────────────────────────────────────────
  { id: 'gi_1', text: 'Have you ever been diagnosed with liver disease, hepatitis, or cirrhosis?', category: GI, type: 'yesno' },
  { id: 'gi_1_type', text: 'Type of liver condition', category: GI, type: 'select', options: ['Hepatitis A', 'Hepatitis B', 'Hepatitis C', 'Fatty Liver (NAFLD/NASH)', 'Cirrhosis', 'Autoimmune Hepatitis', 'Other'], dependsOn: { questionId: 'gi_1', answer: 'Yes' } },
  ...yesFollowUps('gi_1', GI),
  { id: 'gi_2', text: 'Have you ever been diagnosed with Crohn\'s disease or ulcerative colitis?', category: GI, type: 'yesno' },
  ...yesFollowUps('gi_2', GI),
  { id: 'gi_2_surgery', text: 'Have you had any surgery related to this condition?', category: GI, type: 'yesno', dependsOn: { questionId: 'gi_2', answer: 'Yes' } },
  { id: 'gi_3', text: 'Have you ever been diagnosed with pancreatitis?', category: GI, type: 'yesno' },
  ...yesFollowUps('gi_3', GI),
  { id: 'gi_4', text: 'Have you ever been diagnosed with gastroesophageal reflux disease (GERD) or Barrett\'s esophagus?', category: GI, type: 'yesno' },
  ...yesFollowUps('gi_4', GI),
  { id: 'gi_5', text: 'Have you had gallbladder removal or been diagnosed with gallstones?', category: GI, type: 'yesno' },
  { id: 'gi_5_date', text: 'Date of gallbladder surgery', category: GI, type: 'date', dependsOn: { questionId: 'gi_5', answer: 'Yes' } },
  { id: 'gi_6', text: 'Have you ever been diagnosed with a stomach or duodenal ulcer?', category: GI, type: 'yesno' },
  ...yesFollowUps('gi_6', GI),

  // ─── MUSCULOSKELETAL ───────────────────────────────────────────────────────
  { id: 'ms_1', text: 'Have you ever been diagnosed with a chronic back or neck condition (herniated disc, spinal stenosis, degenerative disc disease)?', category: MUSCULO, type: 'yesno' },
  ...yesFollowUps('ms_1', MUSCULO),
  { id: 'ms_1_surgery', text: 'Have you had back or neck surgery?', category: MUSCULO, type: 'yesno', dependsOn: { questionId: 'ms_1', answer: 'Yes' } },
  { id: 'ms_2', text: 'Have you ever been diagnosed with rheumatoid arthritis or osteoarthritis requiring regular treatment?', category: MUSCULO, type: 'yesno' },
  ...yesFollowUps('ms_2', MUSCULO),
  { id: 'ms_3', text: 'Have you ever been diagnosed with fibromyalgia or chronic fatigue syndrome?', category: MUSCULO, type: 'yesno' },
  ...yesFollowUps('ms_3', MUSCULO),
  { id: 'ms_4', text: 'Have you ever been diagnosed with osteoporosis or had a fracture from minimal trauma?', category: MUSCULO, type: 'yesno' },
  ...yesFollowUps('ms_4', MUSCULO),
  { id: 'ms_5', text: 'Have you ever had joint replacement surgery (hip, knee, shoulder)?', category: MUSCULO, type: 'yesno' },
  { id: 'ms_5_joint', text: 'Which joint was replaced?', category: MUSCULO, type: 'select', options: ['Hip', 'Knee', 'Shoulder', 'Multiple Joints'], dependsOn: { questionId: 'ms_5', answer: 'Yes' } },
  { id: 'ms_5_date', text: 'Date of joint replacement', category: MUSCULO, type: 'date', dependsOn: { questionId: 'ms_5', answer: 'Yes' } },

  // ─── GENITOURINARY ─────────────────────────────────────────────────────────
  { id: 'gu_1', text: 'Have you ever been diagnosed with chronic kidney disease or kidney failure?', category: GU, type: 'yesno' },
  ...yesFollowUps('gu_1', GU),
  { id: 'gu_1_dialysis', text: 'Are you on dialysis or have you had a kidney transplant?', category: GU, type: 'yesno', dependsOn: { questionId: 'gu_1', answer: 'Yes' } },
  { id: 'gu_2', text: 'Have you ever been diagnosed with kidney stones recurring more than once?', category: GU, type: 'yesno' },
  ...yesFollowUps('gu_2', GU),
  { id: 'gu_3', text: 'Have you ever been diagnosed with an enlarged prostate (BPH) or elevated PSA?', category: GU, type: 'yesno' },
  ...yesFollowUps('gu_3', GU),
  { id: 'gu_4', text: 'Have you ever been diagnosed with a sexually transmitted infection (STI)?', category: GU, type: 'yesno' },
  ...yesFollowUps('gu_4', GU),

  // ─── BLOOD & IMMUNE ────────────────────────────────────────────────────────
  { id: 'blood_1', text: 'Have you ever been diagnosed with anemia requiring treatment or transfusion?', category: BLOOD, type: 'yesno' },
  ...yesFollowUps('blood_1', BLOOD),
  { id: 'blood_2', text: 'Have you ever been diagnosed with a blood clotting disorder (Factor V Leiden, protein C/S deficiency)?', category: BLOOD, type: 'yesno' },
  ...yesFollowUps('blood_2', BLOOD),
  { id: 'blood_2_anticoag', text: 'Are you on blood thinners (anticoagulant medication)?', category: BLOOD, type: 'yesno', dependsOn: { questionId: 'blood_2', answer: 'Yes' } },
  { id: 'blood_3', text: 'Have you ever tested positive for HIV or been diagnosed with AIDS?', category: BLOOD, type: 'yesno' },
  ...yesFollowUps('blood_3', BLOOD),
  { id: 'blood_3_viral', text: 'Most recent viral load result', category: BLOOD, type: 'select', options: ['Undetectable', 'Low (< 200 copies/mL)', 'Moderate (200-1000)', 'High (> 1000)', 'Unknown'], dependsOn: { questionId: 'blood_3', answer: 'Yes' } },
  { id: 'blood_4', text: 'Have you ever been diagnosed with lupus (SLE) or another autoimmune disorder?', category: BLOOD, type: 'yesno' },
  ...yesFollowUps('blood_4', BLOOD),
  { id: 'blood_5', text: 'Have you ever been diagnosed with sickle cell disease or thalassemia?', category: BLOOD, type: 'yesno' },
  ...yesFollowUps('blood_5', BLOOD),

  // ─── EYES, EARS & SKIN ────────────────────────────────────────────────────
  { id: 'sense_1', text: 'Have you ever been diagnosed with glaucoma, macular degeneration, or retinal detachment?', category: SENSORY, type: 'yesno' },
  ...yesFollowUps('sense_1', SENSORY),
  { id: 'sense_2', text: 'Have you ever experienced sudden or progressive hearing loss?', category: SENSORY, type: 'yesno' },
  ...yesFollowUps('sense_2', SENSORY),
  { id: 'sense_3', text: 'Have you ever been diagnosed with psoriasis, eczema, or a chronic skin condition requiring ongoing treatment?', category: SENSORY, type: 'yesno' },
  ...yesFollowUps('sense_3', SENSORY),
  { id: 'sense_4', text: 'Have you ever had a mole or skin lesion that was biopsied or removed?', category: SENSORY, type: 'yesno' },
  { id: 'sense_4_result', text: 'What was the biopsy result?', category: SENSORY, type: 'select', options: ['Benign', 'Atypical/Dysplastic', 'Malignant (Melanoma)', 'Malignant (Basal Cell)', 'Malignant (Squamous Cell)', 'Awaiting Results'], dependsOn: { questionId: 'sense_4', answer: 'Yes' } },

  // ─── FAMILY MEDICAL HISTORY ────────────────────────────────────────────────
  { id: 'fam_1', text: 'Has any immediate family member (parent, sibling) been diagnosed with heart disease before age 60?', category: FAMILY, type: 'yesno' },
  { id: 'fam_1_who', text: 'Which family member(s)?', category: FAMILY, type: 'text', dependsOn: { questionId: 'fam_1', answer: 'Yes' }, placeholder: 'e.g. Father, age 52' },
  { id: 'fam_2', text: 'Has any immediate family member been diagnosed with cancer before age 60?', category: FAMILY, type: 'yesno' },
  { id: 'fam_2_type', text: 'Type of cancer and which family member', category: FAMILY, type: 'text', dependsOn: { questionId: 'fam_2', answer: 'Yes' }, placeholder: 'e.g. Mother, breast cancer, age 48' },
  { id: 'fam_3', text: 'Has any immediate family member been diagnosed with diabetes?', category: FAMILY, type: 'yesno' },
  { id: 'fam_3_type', text: 'Type (1 or 2) and which family member', category: FAMILY, type: 'text', dependsOn: { questionId: 'fam_3', answer: 'Yes' } },
  { id: 'fam_4', text: 'Has any immediate family member been diagnosed with a stroke before age 60?', category: FAMILY, type: 'yesno' },
  { id: 'fam_4_who', text: 'Which family member and at what age?', category: FAMILY, type: 'text', dependsOn: { questionId: 'fam_4', answer: 'Yes' } },
  { id: 'fam_5', text: 'Has any immediate family member been diagnosed with Huntington\'s disease, polycystic kidney disease, or another hereditary genetic condition?', category: FAMILY, type: 'yesno' },
  { id: 'fam_5_condition', text: 'Which condition and family member?', category: FAMILY, type: 'text', dependsOn: { questionId: 'fam_5', answer: 'Yes' } },
  { id: 'fam_6', text: 'Has any immediate family member died before age 60 from natural causes?', category: FAMILY, type: 'yesno' },
  { id: 'fam_6_detail', text: 'Cause of death, relationship, and age at death', category: FAMILY, type: 'textarea', dependsOn: { questionId: 'fam_6', answer: 'Yes' } },
  { id: 'fam_7', text: 'Has any immediate family member been diagnosed with a mental health disorder?', category: FAMILY, type: 'yesno' },
  { id: 'fam_7_detail', text: 'Condition and which family member', category: FAMILY, type: 'text', dependsOn: { questionId: 'fam_7', answer: 'Yes' } },
  { id: 'fam_8', text: 'Has any immediate family member been diagnosed with substance abuse or addiction?', category: FAMILY, type: 'yesno' },

  // ─── MEDICATIONS & HOSPITALIZATIONS ────────────────────────────────────────
  { id: 'med_1', text: 'Are you currently taking any prescription medications?', category: MEDS, type: 'yesno' },
  { id: 'med_1_list', text: 'List all current prescription medications, dosages, and reasons', category: MEDS, type: 'textarea', dependsOn: { questionId: 'med_1', answer: 'Yes' }, placeholder: 'e.g. Lisinopril 10mg daily for blood pressure\nMetformin 500mg twice daily for diabetes' },
  { id: 'med_2', text: 'Are you currently taking any controlled substances (narcotics, benzodiazepines, stimulants)?', category: MEDS, type: 'yesno' },
  { id: 'med_2_list', text: 'List all controlled substances with dosage and prescribing physician', category: MEDS, type: 'textarea', dependsOn: { questionId: 'med_2', answer: 'Yes' } },
  { id: 'med_3', text: 'Have you been hospitalized or had surgery within the past 5 years (other than those already mentioned)?', category: MEDS, type: 'yesno' },
  { id: 'med_3_detail', text: 'Describe each hospitalization or surgery', category: MEDS, type: 'textarea', dependsOn: { questionId: 'med_3', answer: 'Yes' }, placeholder: 'Date, facility, reason, and outcome for each' },
  { id: 'med_4', text: 'Do you have any scheduled surgeries, procedures, or medical tests pending?', category: MEDS, type: 'yesno' },
  { id: 'med_4_detail', text: 'Describe the pending procedures', category: MEDS, type: 'textarea', dependsOn: { questionId: 'med_4', answer: 'Yes' } },
  { id: 'med_5', text: 'Have you visited an emergency room more than twice in the past 12 months?', category: MEDS, type: 'yesno' },
  { id: 'med_5_detail', text: 'Describe the reasons for emergency visits', category: MEDS, type: 'textarea', dependsOn: { questionId: 'med_5', answer: 'Yes' } },
  { id: 'med_6', text: 'Have you ever been advised by a physician to have a test, treatment, or surgery that you did not complete?', category: MEDS, type: 'yesno' },
  { id: 'med_6_detail', text: 'Describe the recommended test or treatment and why it was not completed', category: MEDS, type: 'textarea', dependsOn: { questionId: 'med_6', answer: 'Yes' } },
  { id: 'med_7', text: 'Do you require the use of a wheelchair, walker, prosthetic device, or home medical equipment?', category: MEDS, type: 'yesno' },
  { id: 'med_7_detail', text: 'Describe the device and reason for use', category: MEDS, type: 'textarea', dependsOn: { questionId: 'med_7', answer: 'Yes' } },
];

export const HEALTH_CATEGORIES = [
  GENERAL, CARDIO, RESPIRATORY, CANCER, NEURO, MENTAL,
  ENDOCRINE, GI, MUSCULO, GU, BLOOD, SENSORY, FAMILY, MEDS,
];

export function getVisibleQuestions(answers: Record<string, string>): HealthQuestion[] {
  return HEALTH_QUESTIONS.filter(q => {
    if (!q.dependsOn) return true;
    return answers[q.dependsOn.questionId] === q.dependsOn.answer;
  });
}

export function getQuestionsByCategory(category: string, answers: Record<string, string>): HealthQuestion[] {
  return getVisibleQuestions(answers).filter(q => q.category === category);
}
