import React from 'react';
import { Typography } from 'antd';

const { Title } = Typography;

interface PageTitleProps {
  title: string;
  count?: number;
  label?: string;
  extra?: React.ReactNode;
}

const PageTitle: React.FC<PageTitleProps> = ({ title, count, label, extra }) => {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: 20 }}>
      <Title level={3} style={{ margin: 0, fontWeight: 600, fontSize: 22 }}>
        {title}
        {count !== undefined && label && (
          <span style={{ fontSize: 13, fontWeight: 400, color: 'var(--text-secondary)', marginLeft: 12 }}>
            {count} {label}
          </span>
        )}
      </Title>
      {extra}
    </div>
  );
};

export default PageTitle;