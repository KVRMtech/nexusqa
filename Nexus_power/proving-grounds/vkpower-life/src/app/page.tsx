'use client';

import Link from 'next/link';
import Header from '@/components/Header';

export default function HomePage() {
  return (
    <>
      <Header />
      <main className="flex-1">
        <section className="bg-navy text-white">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-20 md:py-28">
            <div className="max-w-2xl">
              <div className="inline-flex items-center gap-2 bg-white/10 backdrop-blur rounded-full px-4 py-1.5 text-xs font-semibold tracking-wide text-gold-200 mb-6">
                <span className="w-2 h-2 rounded-full bg-gold animate-pulse" />
                Protecting Military Families Since 1922
              </div>
              <h1 className="text-4xl md:text-5xl font-extrabold leading-tight tracking-tight">
                Life insurance built for those who serve
              </h1>
              <p className="mt-5 text-lg text-gray-300 leading-relaxed max-w-xl">
                Affordable, comprehensive life insurance for active duty service members,
                veterans, and their families. Get a quote in minutes — apply entirely online.
              </p>
              <div className="mt-8 flex flex-wrap gap-4">
                <Link href="/life-insurance/quote/start" className="btn-gold text-base px-8 py-3">
                  Get a Free Quote
                </Link>
                <Link href="/login" className="btn-outline !border-white/30 !text-white hover:!bg-white/10 text-base px-8 py-3">
                  Member Sign In
                </Link>
              </div>
            </div>
          </div>
        </section>

        <section className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-16">
          <h2 className="text-2xl font-bold text-navy text-center mb-12">Coverage options for every stage of life</h2>
          <div className="grid grid-cols-1 md:grid-cols-2 lg:grid-cols-4 gap-6">
            {[
              { title: 'Term Life', desc: 'Affordable protection for a set period — 10, 15, 20, 25, or 30 years.', price: 'From $12/mo' },
              { title: 'Whole Life', desc: 'Lifetime coverage that builds cash value you can borrow against.', price: 'From $45/mo' },
              { title: 'Universal Life', desc: 'Flexible premiums and adjustable death benefit to fit your needs.', price: 'From $38/mo' },
              { title: 'Variable Universal', desc: 'Investment options with life insurance protection in one policy.', price: 'From $52/mo' },
            ].map(p => (
              <div key={p.title} className="card hover:shadow-md transition-shadow">
                <h3 className="text-lg font-bold text-navy mb-2">{p.title}</h3>
                <p className="text-sm text-gray-600 mb-4 leading-relaxed">{p.desc}</p>
                <p className="text-gold font-extrabold text-lg">{p.price}</p>
              </div>
            ))}
          </div>
        </section>

        <section className="bg-white border-t border-gray-200">
          <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 py-16">
            <div className="grid grid-cols-2 md:grid-cols-4 gap-8 text-center">
              {[
                { stat: '$50B+', label: 'Coverage In Force' },
                { stat: '2.4M', label: 'Members Served' },
                { stat: 'A++', label: 'AM Best Rating' },
                { stat: '98.7%', label: 'Claims Paid Ratio' },
              ].map(s => (
                <div key={s.label}>
                  <div className="text-3xl md:text-4xl font-black text-navy">{s.stat}</div>
                  <div className="text-sm text-gray-500 mt-1">{s.label}</div>
                </div>
              ))}
            </div>
          </div>
        </section>
      </main>

      <footer className="bg-navy-800 text-gray-400 text-xs py-6">
        <div className="max-w-7xl mx-auto px-4 sm:px-6 lg:px-8 flex flex-wrap justify-between gap-4">
          <span>VKPower Life Insurance Company. Demonstration environment — no real policies are issued.</span>
          <span>&copy; {new Date().getFullYear()} VKPower</span>
        </div>
      </footer>
    </>
  );
}
